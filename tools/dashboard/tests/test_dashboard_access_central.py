"""Focused proof for the first Settings-native production approval kind."""

from __future__ import annotations

import asyncio
from pathlib import Path
import stat
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import api_auth, approvals_routes, attention_routes, unlock_routes
from tools.dashboard.approval_http_bridge import (
    ApprovalHttpBridge,
    ApprovalHttpRegistry,
)
from tools.dashboard.approval_kind_registry import build_production_registry
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    HumanApprovalActor,
    InMemoryApprovalStore,
)
from tools.dashboard import dashboard_access_central as central
from tools.network.idkit.canonical import canonical_json
from tools.network.idkit.keys import KeyPair
from tools.network import fleet_sync_scheduler
from tools.data_paths import resolve_store
from tools.graph import cli as graph_cli


ROOT = KeyPair.generate()
EPHEMERAL = KeyPair.generate()
APPROVAL_ID = "central-dashboard-access-test-0001"
DESTINATION = "d" * 43
NONCE = "a" * 64


def _principal(subject="auto-requester"):
    return api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.LOCAL_SESSION,
        subject=subject,
    )


def _composition(*, clock=lambda: 1000.75, destination=DESTINATION):
    runtime = central.build_approval_runtime(
        nonce_factory=lambda: NONCE,
        destination_resolver=lambda: destination,
        root_resolver=lambda: ROOT.public_hex,
    )
    registry = build_production_registry(runtimes={central.KIND: runtime})
    approvals = ApprovalService(
        registry=registry,
        store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: ROOT.public_hex,
        session_label_resolver=lambda subject: f"Session {subject}",
        clock=clock,
        id_factory=lambda: APPROVAL_ID,
    )
    return approvals, registry


def _create(approvals):
    return approvals.create_from_principal(
        central.KIND,
        _principal(),
        {"ephemeral_pub": EPHEMERAL.public_hex},
    )


def _grant_decision(request):
    grant = request.payload["staged"]["grant"]
    return {
        "grant": grant,
        "signature": ROOT.sign_hex(
            central.GRANT_SIGNING_DOMAIN + canonical_json(grant)
        ),
    }


def test_only_dashboard_access_is_activated_in_production_composition():
    approvals, registry = _composition()
    assert approvals.registry is registry
    assert [kind for kind, row in registry.kinds.items() if row.runtime is not None] == [
        central.KIND
    ]
    with pytest.raises(ValueError):
        build_production_registry(runtimes={"invented": registry.kinds[central.KIND].runtime})


def test_request_freezes_opaque_requester_destination_and_public_grant():
    approvals, registry = _composition()
    request = _create(approvals)
    payload = request.payload
    grant = payload["staged"]["grant"]
    assert payload["application_scope"] == "sessions"
    assert payload["request"] == {"ephemeral_pub": EPHEMERAL.public_hex}
    assert payload["staged"]["result_destination_id"] == DESTINATION
    assert len(payload["requester_ref"]["id"]) == 43
    assert payload["requester_ref"]["id"] not in str(_principal().subject)
    assert grant == {
        "v": 1,
        "nonce": NONCE,
        "grantee": payload["requester_ref"]["id"],
        "ephemeral_pub": EPHEMERAL.public_hex,
        "scope": ["dashboard:ui"],
        "issued_at": 1000,
        "expires_at": 8200,
    }
    assert payload["expires_at"] == pytest.approx(8200.75)
    assert payload["safe_review"]["grant"] == grant
    serialized = str(payload)
    for forbidden in ("private", "password", "cookie", "bearer", "seed"):
        assert forbidden not in serialized.lower()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"ephemeral_pub": "bad"},
        {"ephemeral_pub": EPHEMERAL.public_hex, "session": "forged"},
        {"ephemeral_pub": EPHEMERAL.public_hex, "scope": ["admin"]},
    ],
)
def test_request_rejects_forged_or_malformed_input(body):
    approvals, registry = _composition()
    with pytest.raises(ApprovalServiceError, match="invalid_request"):
        approvals.create_from_principal(central.KIND, _principal(), body)
    assert approvals.store.get_request(APPROVAL_ID) is None


def test_signed_grant_uses_exact_service_decision_time_and_first_winner():
    approvals, registry = _composition()
    request = _create(approvals)
    decision = _grant_decision(request)
    answer = approvals.decide(
        request.approval_id,
        HumanApprovalActor._verified(ROOT.public_hex),
        outcome="granted",
        decision=decision,
        now=8199.5,
    )
    assert answer.payload["resolved_at"] == 8199.5
    assert answer.payload["decision"] == decision
    assert approvals.decide(
        request.approval_id,
        HumanApprovalActor._verified(ROOT.public_hex),
        outcome="declined",
        decision={},
        now=8199.6,
    ) == answer


def test_grant_subsecond_tail_is_invalid_without_resolution():
    approvals, registry = _composition()
    request = _create(approvals)
    with pytest.raises(ApprovalServiceError, match="invalid_decision"):
        approvals.decide(
            request.approval_id,
            HumanApprovalActor._verified(ROOT.public_hex),
            outcome="granted",
            decision=_grant_decision(request),
            now=8200.25,
        )
    assert approvals.store.get_resolution(request.approval_id) is None


def test_inbox_text_names_the_requesting_session():
    approvals, registry = _composition()
    request = _create(approvals)
    assert central.inbox_text(approvals.status(request.approval_id)) == (
        "Dashboard access requested",
        "Session auto-requester wants temporary Dashboard access.",
    )


def test_destination_bound_consumer_writes_only_matching_local_grant(monkeypatch):
    approvals, registry = _composition()
    request = _create(approvals)
    approvals.decide(
        request.approval_id,
        HumanApprovalActor._verified(ROOT.public_hex),
        outcome="granted",
        decision=_grant_decision(request),
        now=1002.0,
    )
    status = approvals.status(request.approval_id)
    stored = {}

    def write(**values):
        stored.update(values)

    def read(nonce):
        assert nonce == NONCE
        if not stored:
            return None
        return {
            "approval_id": stored["approval_id"],
            "ephemeral_pub": stored["ephemeral_pub"],
            "operator_signature": stored["operator_signature"],
            "grantee": stored["grantee"],
            "scope": stored["scope"],
            "issued_at": stored["issued_at"],
            "expires_at": stored["expires_at"],
        }

    monkeypatch.setattr(central.identity_sessions, "store_access_grant", write)
    monkeypatch.setattr(central.identity_sessions, "get_access_grant", read)
    wrong = central.DashboardAccessResultConsumer(
        root_resolver=lambda: ROOT.public_hex,
        destination_resolver=lambda: "x" * 43,
    )
    assert wrong.materialize(status) is False
    assert stored == {}
    matching = central.DashboardAccessResultConsumer(
        root_resolver=lambda: ROOT.public_hex,
        destination_resolver=lambda: DESTINATION,
        clock=lambda: 1003.0,
    )
    assert matching.materialize(status) is True
    assert stored["approval_id"] == APPROVAL_ID
    result = matching.project(status)
    assert result["approved"] is True
    assert result["execution"] == {"ok": True}


def test_matching_application_result_survives_local_store_reopen(
    tmp_path, monkeypatch,
):
    identity_db = tmp_path / "dashboard-realm" / "sessions.db"
    monkeypatch.setenv("DASHBOARD_IDENTITY_SESSION_DB", str(identity_db))
    central.identity_sessions.reset_for_tests()
    secret = b"s" * 32
    destination = central.dashboard_access_result_destination_id(secret)
    approvals, registry = _composition(
        destination=destination,
    )
    request = _create(approvals)
    approvals.decide(
        request.approval_id,
        HumanApprovalActor._verified(ROOT.public_hex),
        outcome="granted",
        decision=_grant_decision(request),
        now=1002.0,
    )
    consumer = central.DashboardAccessResultConsumer(
        root_resolver=lambda: ROOT.public_hex,
        destination_resolver=lambda: destination,
        clock=lambda: 1003.0,
    )
    status = approvals.status(request.approval_id)
    try:
        assert consumer.materialize(status) is True
        nonce = request.payload["staged"]["grant"]["nonce"]
        first = central.identity_sessions.get_access_grant(nonce)
        assert first is not None and first["approval_id"] == APPROVAL_ID
        central.identity_sessions.reset_for_tests()
        assert consumer.materialize(status) is True
        assert central.identity_sessions.get_access_grant(nonce) == first
    finally:
        central.identity_sessions.reset_for_tests()


def test_application_store_failure_keeps_central_grant_retryable(monkeypatch):
    approvals, registry = _composition()
    request = _create(approvals)
    resolution = approvals.decide(
        request.approval_id,
        HumanApprovalActor._verified(ROOT.public_hex),
        outcome="granted",
        decision=_grant_decision(request),
        now=1002.0,
    )
    consumer = central.DashboardAccessResultConsumer(
        root_resolver=lambda: ROOT.public_hex,
        destination_resolver=lambda: DESTINATION,
        clock=lambda: 1003.0,
    )
    attempts = 0
    stored = {}

    def write(**values):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("local grant store unavailable")
        stored.update(values)

    monkeypatch.setattr(central.identity_sessions, "store_access_grant", write)
    monkeypatch.setattr(
        central.identity_sessions,
        "get_access_grant",
        lambda _nonce: None if not stored else {
            "approval_id": stored["approval_id"],
            "ephemeral_pub": stored["ephemeral_pub"],
            "operator_signature": stored["operator_signature"],
            "grantee": stored["grantee"],
            "scope": stored["scope"],
            "issued_at": stored["issued_at"],
            "expires_at": stored["expires_at"],
        },
    )
    status = approvals.status(request.approval_id)
    with pytest.raises(RuntimeError, match="unavailable"):
        consumer.materialize(status)
    assert approvals.status(request.approval_id).resolution == resolution
    assert consumer.materialize(status) is True
    assert attempts == 2


def test_missing_application_result_is_not_created_after_grant_expiry(monkeypatch):
    approvals, registry = _composition()
    request = _create(approvals)
    approvals.decide(
        request.approval_id,
        HumanApprovalActor._verified(ROOT.public_hex),
        outcome="granted",
        decision=_grant_decision(request),
        now=1002.0,
    )
    writes = []
    monkeypatch.setattr(
        central.identity_sessions,
        "get_access_grant",
        lambda _nonce: None,
    )
    monkeypatch.setattr(
        central.identity_sessions,
        "store_access_grant",
        lambda **values: writes.append(values),
    )
    consumer = central.DashboardAccessResultConsumer(
        root_resolver=lambda: ROOT.public_hex,
        destination_resolver=lambda: DESTINATION,
        clock=lambda: 8200.0,
    )
    status = approvals.status(request.approval_id)
    assert consumer.materialize(status) is False
    assert consumer.project(status) is None
    assert writes == []


def test_timely_existing_application_result_remains_idempotent_after_expiry(
    monkeypatch,
):
    approvals, registry = _composition()
    request = _create(approvals)
    approvals.decide(
        request.approval_id,
        HumanApprovalActor._verified(ROOT.public_hex),
        outcome="granted",
        decision=_grant_decision(request),
        now=1002.0,
    )
    status = approvals.status(request.approval_id)
    grant = request.payload["staged"]["grant"]
    decision = status.resolution.payload["decision"]
    existing = {
        "approval_id": APPROVAL_ID,
        "ephemeral_pub": grant["ephemeral_pub"],
        "operator_signature": decision["signature"],
        "grantee": grant["grantee"],
        "scope": grant["scope"],
        "issued_at": grant["issued_at"],
        "expires_at": grant["expires_at"],
    }
    writes = []
    monkeypatch.setattr(
        central.identity_sessions,
        "get_access_grant",
        lambda nonce: existing if nonce == NONCE else None,
    )
    monkeypatch.setattr(
        central.identity_sessions,
        "store_access_grant",
        lambda **values: writes.append(values),
    )
    consumer = central.DashboardAccessResultConsumer(
        root_resolver=lambda: ROOT.public_hex,
        destination_resolver=lambda: DESTINATION,
        clock=lambda: 9999.0,
    )
    assert consumer.materialize(status) is True
    assert consumer.project(status)["execution"] == {"ok": True}
    assert writes == []


def _coordinator(approvals):
    return central.DashboardAccessCoordinator(
        approvals=approvals,
        consumer=central.DashboardAccessResultConsumer(
            root_resolver=lambda: ROOT.public_hex,
            destination_resolver=lambda: "x" * 43,
        ),
    )


def test_coordinator_reconciles_a_synced_approval_on_its_event(monkeypatch):
    approvals, registry = _composition()
    request = _create(approvals)
    coordinator = _coordinator(approvals)
    calls = []
    monkeypatch.setattr(coordinator, "reconcile_exact", calls.append)

    async def exercise():
        await coordinator.start()
        coordinator.offer_synced(addresses=[central.SyncedSettingsAddress(
            set_id="dashboard.approval.request",
            schema_revision=1,
            key=request.approval_id,
        )])
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        await coordinator.stop()

    asyncio.run(exercise())
    assert calls == [request.approval_id]


def test_coordinator_neither_scans_nor_retries(monkeypatch):
    approvals, registry = _composition()
    request = _create(approvals)
    coordinator = _coordinator(approvals)
    calls = []

    def failing(approval_id):
        calls.append(approval_id)
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(coordinator, "reconcile_exact", failing)

    async def exercise():
        await coordinator.start()
        await asyncio.sleep(0.05)
        # Starting reads nothing: no startup scan.
        assert calls == []
        coordinator.offer(request.approval_id)
        await asyncio.sleep(0.2)
        await coordinator.stop()
        coordinator.offer(request.approval_id)
        await asyncio.sleep(0.05)

    asyncio.run(exercise())
    # One event, one attempt: a failure is logged, never retried, and an
    # event after stop is ignored.
    assert calls == [request.approval_id]


def test_session_secret_is_manifest_rooted_with_identity_store(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTONOMY_DATA_ROOT", raising=False)
    monkeypatch.setenv(
        "DASHBOARD_SESSION_SECRET_FILE",
        str(tmp_path / "forbidden-split.secret"),
    )
    identity_db = tmp_path / "custom-realm" / "sessions.db"
    monkeypatch.setenv("DASHBOARD_IDENTITY_SESSION_DB", str(identity_db))
    assert resolve_store("dashboard_session_secret") == identity_db.with_name(
        "dashboard-session.secret"
    )
    monkeypatch.delenv("DASHBOARD_IDENTITY_SESSION_DB")
    ambient = tmp_path / "ambient"
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(ambient))
    assert resolve_store("dashboard_session_secret") == (
        ambient / "dashboard-session.secret"
    )
    monkeypatch.delenv("AUTONOMY_DATA_ROOT")
    hermetic = tmp_path / "hermetic"
    assert resolve_store("dashboard_session_secret", root=hermetic) == (
        hermetic / "dashboard-session.secret"
    )


def test_personal_sync_hint_is_payload_free_bounded_and_best_effort():
    received = []
    fleet_sync_scheduler.set_settings_materialization_hook(
        lambda **values: received.append(values)
    )
    try:
        items = [SimpleNamespace(mutation=SimpleNamespace(
            table="settings",
            address=("dashboard.approval.request", 1, "central-one"),
        ))]
        fleet_sync_scheduler._emit_settings_materialized(items)
        assert received[0]["gap"] is False
        assert received[0]["addresses"] == (
            fleet_sync_scheduler.MaterializedSettingsAddress(
                "dashboard.approval.request", 1, "central-one"
            ),
        )
        assert "payload" not in received[0]["addresses"][0].__dataclass_fields__

        received.clear()
        many = [SimpleNamespace(mutation=SimpleNamespace(
            table="settings",
            address=("test.set", 1, f"key-{index}"),
        )) for index in range(257)]
        fleet_sync_scheduler._emit_settings_materialized(many)
        assert received == [{"addresses": (), "gap": True}]

        fleet_sync_scheduler.set_settings_materialization_hook(
            lambda **_values: (_ for _ in ()).throw(RuntimeError("observer failed"))
        )
        fleet_sync_scheduler._emit_settings_materialized(items)
    finally:
        fleet_sync_scheduler.set_settings_materialization_hook(None)




def test_graph_session_auth_completes_central_create_wait_materialize_and_redeem(
    tmp_path, monkeypatch, capsys,
):
    now = 2_000_000_000.25
    approvals, registry = _composition(
        clock=lambda: now,
    )
    consumer = central.DashboardAccessResultConsumer(
        root_resolver=lambda: ROOT.public_hex,
        destination_resolver=lambda: DESTINATION,
        clock=lambda: now + 1,
    )
    coordinator = central.DashboardAccessCoordinator(
        approvals=approvals,
        consumer=consumer,
    )
    bridge = ApprovalHttpBridge(
        approvals=approvals,
        registry=ApprovalHttpRegistry(
            approvals=approvals.registry,
            adapters={
                central.KIND: central.build_http_adapter(
                    consumer,
                    reconcile=coordinator.reconcile_exact,
                ),
            },
        ),
    )
    runtime = attention_routes.AttentionRouteRuntime(
        approvals=approvals,
        hub=attention_routes.PrivateAttentionHub(),
        inbox_texts={central.KIND: central.inbox_text},
        approval_http=bridge,
        approval_reconciler=coordinator,
    )
    previous = attention_routes.configure_runtime(runtime)
    identity_db = tmp_path / "dashboard-realm" / "sessions.db"
    jar = tmp_path / "dashboard.cookies"
    monkeypatch.setenv("DASHBOARD_IDENTITY_SESSION_DB", str(identity_db))
    monkeypatch.setenv("GRAPH_API", "https://dashboard.test")
    monkeypatch.setattr(graph_cli, "_resolve_crosstalk_token", lambda: "test-token")
    monkeypatch.setattr(graph_cli, "_get_session_name", lambda: "auto-live-proof")
    monkeypatch.setattr(unlock_routes, "_now", lambda: now + 2)
    monkeypatch.setattr(
        api_auth,
        "principal_from_request",
        lambda _request: _principal("auto-live-proof"),
    )
    central.identity_sessions.reset_for_tests()
    decided = False

    class _UrlResponse:
        def __init__(self, response):
            self._response = response
            self.headers = response.headers

        def read(self):
            return self._response.content

    try:
        with TestClient(
            Starlette(routes=approvals_routes.ROUTES + unlock_routes.ROUTES),
            base_url="https://dashboard.test",
        ) as client:
            def urlopen(request, **_kwargs):
                nonlocal decided
                parsed = urlsplit(request.full_url)
                path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                if parsed.path == f"/api/approvals/{APPROVAL_ID}" and not decided:
                    pending = approvals.status(APPROVAL_ID).request
                    approvals.decide(
                        APPROVAL_ID,
                        HumanApprovalActor._verified(ROOT.public_hex),
                        outcome="granted",
                        decision=_grant_decision(pending),
                        now=now + 0.5,
                    )
                    decided = True
                response = client.request(
                    request.get_method(),
                    path,
                    content=request.data,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer test-token",
                    },
                )
                assert response.status_code < 400, response.text
                return _UrlResponse(response)

            monkeypatch.setattr("urllib.request.urlopen", urlopen)
            graph_cli.cmd_session_auth(SimpleNamespace(
                wait=5,
                jar=str(jar),
                browser=False,
            ))

        grant = central.identity_sessions.get_access_grant(NONCE)
        assert decided is True
        assert grant is not None and grant["approval_id"] == APPROVAL_ID
        assert grant["consumed_at"] == pytest.approx(now + 2)
        assert stat.S_IMODE(jar.stat().st_mode) == 0o600
        jar_text = jar.read_text()
        assert "autonomy_dashboard_session" in jar_text
        cookie_value = jar_text.rsplit("\t", 1)[-1].strip()
        captured = capsys.readouterr()
        assert cookie_value and cookie_value not in captured.out + captured.err
        assert "dashboard session granted" in captured.out
        assert approvals.status(APPROVAL_ID).resolution.payload["outcome"] == "granted"
    finally:
        attention_routes.configure_runtime(previous)
        bridge.close()
        central.identity_sessions.reset_for_tests()
