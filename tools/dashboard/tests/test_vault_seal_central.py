"""Central ``vault_seal``: an agent asks the operator to vault a secret.

Covers the kind end to end without a browser: the planner freezes the
bearer-derived destination and the proven requesting session, the deposit
route seals the typed value into that destination, the validator refuses a
grant that names anything but a row there, the requester's envelope and the
wake carry no value, and the inbox row goes from needs-attention to resolved.
"""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import api_auth, approval_service, attention_routes, vault_routes
from tools.dashboard import vault_seal_central as central
from tools.dashboard.approval_http_bridge import (
    ApprovalHttpBridge,
    ApprovalHttpBridgeError,
    ApprovalHttpRegistry,
)
from tools.dashboard.approval_kind_registry import build_production_registry
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    HumanApprovalActor,
    InMemoryApprovalStore,
)
from tools.dashboard.tests._serving_vault_kit import publish_recipient
from tools.graph import ops, settings_ops
from tools.graph.schemas.central_attention import ApprovalRequestV1
from tools.graph.schemas.vault_credential import (
    VAULT_AUDITED_SET_ID,
    VAULT_CREDENTIAL_REVISION,
    VAULT_SECURED_SET_ID,
)
from tools.graph.tests.vault_read_harness import VaultWorld, clear_seams
from tools.network.idkit.keys import KeyPair
from tools.vault.store import VaultStore


ROOT = KeyPair.generate()
APPROVAL_ID = "central-vault-seal-test-000000000001"
SECRET = "ghp_" + "x" * 36
REQUEST = {
    "name": "github.token",
    "tier": "audited",
    "description": "Personal access token for pushing release tags from CI.",
}


def _org_principal(subject="auto-real", org="autonomy"):
    return api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.ORG_SESSION, subject=subject, org=org,
    )


def _local_principal(subject="auto-local"):
    return api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, subject=subject)


@pytest.fixture
def personal_store(tmp_path, monkeypatch):
    """A pinned personal store that the vault credential sets resolve to."""
    graph_db = tmp_path / "personal.db"
    monkeypatch.setenv("GRAPH_DB", str(graph_db))
    monkeypatch.delenv("GRAPH_API", raising=False)
    from tools.graph import db as graph_db_module

    original = graph_db_module._org_db_path
    monkeypatch.setattr(
        graph_db_module, "_org_db_path",
        lambda org, root=None: (
            graph_db if org == "personal"
            else (graph_db.parent / "machine.db") if org == "machine"
            else original(org, root)
        ),
    )
    publish_recipient(monkeypatch)
    world = VaultWorld(tmp_path / "vault").register()
    world.mint_policy_class()
    with VaultStore(graph_db) as store:
        store.put_class(world.policy_class)
    try:
        yield graph_db, world
    finally:
        world.close()
        clear_seams()


def _composition(*, clock=lambda: 1000.5):
    registry = build_production_registry(runtimes={central.KIND: central.build_approval_runtime()})
    approvals = ApprovalService(
        registry=registry,
        store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: ROOT.public_hex,
        session_label_resolver=lambda subject: f"{subject} · Release pipeline",
        clock=clock,
        id_factory=lambda: APPROVAL_ID,
    )
    bridge = ApprovalHttpBridge(
        approvals=approvals,
        registry=ApprovalHttpRegistry(
            approvals=registry, adapters={central.KIND: central.build_http_adapter()},
        ),
    )
    runtime = attention_routes.AttentionRouteRuntime(
        approvals=approvals,
        hub=attention_routes.PrivateAttentionHub(),
        inbox_texts={central.KIND: central.inbox_text},
        approval_http=bridge,
        operator_result_projectors={central.KIND: central.project_result},
    )
    return approvals, bridge, runtime


@pytest.fixture
def central_runtime(personal_store):
    _graph_db, world = personal_store
    approvals, bridge, runtime = _composition()
    previous = attention_routes.configure_runtime(runtime)
    try:
        yield approvals, bridge, world
    finally:
        attention_routes.configure_runtime(previous)


def _actor():
    return HumanApprovalActor._verified(ROOT.public_hex)


# ── planning ────────────────────────────────────────────────────────────


def test_org_session_request_freezes_namespaced_destination_and_session(personal_store):
    approvals, _bridge, _runtime = _composition()
    record = approvals.create_from_principal(central.KIND, _org_principal(), dict(REQUEST))
    payload = record.payload
    ApprovalRequestV1.validate(payload)
    assert payload["application_scope"] == "vault"
    assert payload["staged"] == {
        "v": 1, "set_id": VAULT_AUDITED_SET_ID, "key": "autonomy:github.token",
        "tier": "audited", "session": "auto-real",
    }
    assert payload["request"] == {**REQUEST, "replace": False}
    assert payload["subject_ref"] == f"vault-seal:{VAULT_AUDITED_SET_ID}/autonomy:github.token"
    review = payload["safe_review"]
    assert review["approval_id"] == APPROVAL_ID
    assert review["name"] == "github.token"
    assert review["key"] == "autonomy:github.token"
    assert review["tier"] == "audited"
    assert review["tier_label"] == central.TIER_LABELS["audited"]
    assert review["detail"] == REQUEST["description"]
    assert review["requester_label"] == "auto-real · Release pipeline"
    # NEVER expires: the request waits in Central for the operator.
    assert "expires_at" not in payload
    assert "value" not in json.dumps(payload)
    title, summary = central.inbox_text(approvals.status(APPROVAL_ID))
    assert title == "Vault a secret: github.token"
    assert summary == "auto-real · Release pipeline needs github.token vaulted."


def test_local_session_request_uses_the_bare_personal_key(personal_store):
    approvals, _bridge, _runtime = _composition()
    record = approvals.create_from_principal(
        central.KIND, _local_principal(), {**REQUEST, "tier": "secured"},
    )
    assert record.payload["staged"]["key"] == "github.token"
    assert record.payload["staged"]["set_id"] == VAULT_SECURED_SET_ID
    assert record.payload["staged"]["session"] == "auto-local"


@pytest.mark.parametrize("body,reason", [
    ({**REQUEST, "name": "autonomy:github.token"}, "unprefixed"),
    ({**REQUEST, "name": "bad name"}, "1-128 characters"),
    ({**REQUEST, "tier": "plaintext"}, "tier"),
    ({**REQUEST, "description": "   "}, "description"),
    ({**REQUEST, "purpose": "smuggled field"}, "accepts only"),
    ({**REQUEST, "replace": "yes"}, "replace"),
    ({"name": "github.token"}, "description"),
])
def test_request_rejects_malformed_or_forged_input(personal_store, body, reason):
    approvals, _bridge, _runtime = _composition()
    with pytest.raises(ApprovalServiceError, match="invalid_request") as info:
        approvals.create_from_principal(central.KIND, _org_principal(), body)
    assert reason in str(info.value.__cause__)
    assert approvals.store.get_request(APPROVAL_ID) is None


def test_request_refuses_a_name_that_already_exists_unless_replacing(personal_store):
    ops.add_setting(
        VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION, "autonomy:github.token",
        {"value": "old"}, org=ops.CALLER_ORG,
    )
    approvals, _bridge, _runtime = _composition()
    # Tier is immutable per name: the other tier is refused outright.
    with pytest.raises(ApprovalServiceError) as info:
        approvals.create_from_principal(
            central.KIND, _org_principal(), {**REQUEST, "tier": "secured"},
        )
    assert "already exists at the audited tier" in str(info.value.__cause__)
    with pytest.raises(ApprovalServiceError) as info:
        approvals.create_from_principal(central.KIND, _org_principal(), dict(REQUEST))
    assert "pass replace" in str(info.value.__cause__)
    record = approvals.create_from_principal(
        central.KIND, _org_principal(), {**REQUEST, "replace": True},
    )
    assert record.payload["safe_review"]["replace"] is True


# ── deposit + decision ──────────────────────────────────────────────────


def _deposit_client(monkeypatch, actor=None):
    monkeypatch.setattr(
        approval_service, "resolve_human_approval_actor",
        lambda _request: actor or _actor(),
    )
    return TestClient(Starlette(routes=vault_routes.ROUTES), base_url="https://localhost:8080")


def test_deposit_seals_into_the_frozen_destination_and_grant_names_that_row(
    central_runtime, monkeypatch,
):
    approvals, bridge, _world = central_runtime
    woken: list = []
    coordinator = central.VaultSealCoordinator(
        approvals=approvals, notify=lambda session, spec: woken.append((session, spec)) or "accepted",
    )

    approval_id = bridge.create(central.KIND, _org_principal(), dict(REQUEST))
    assert approval_id == APPROVAL_ID
    # The request is already an inbox row the operator can open.
    items = attention_routes._inbox_items()
    assert [item.approval_id for item in items] == [APPROVAL_ID]
    safe = attention_routes._safe_item(items[0])
    assert safe["attention_state"] == "needs_attention"
    assert safe["title"] == "Vault a secret: github.token"
    assert safe["open"]["renderer_id"] == central.RENDERER_ID
    assert SECRET not in json.dumps(safe)
    # A change event before any decision wakes nobody.
    assert coordinator.reconcile_exact(APPROVAL_ID) is not None
    assert woken == []

    # A grant that names no deposited row is refused before anything commits.
    with pytest.raises(ApprovalServiceError, match="invalid_decision"):
        approvals.decide(
            APPROVAL_ID, _actor(), outcome="granted", decision={"setting_id": "not-a-row-here"},
        )
    assert approvals.store.get_resolution(APPROVAL_ID) is None

    client = _deposit_client(monkeypatch)
    with client:
        assert client.post(f"/api/vault/deposit/{APPROVAL_ID}", json={"value": ""}).status_code == 400
        assert client.post(
            f"/api/vault/deposit/{APPROVAL_ID}", json={"value": SECRET, "tier": "secured"},
        ).status_code == 400
        assert client.post(
            "/api/vault/deposit/central-missing-000000000000000", json={"value": SECRET},
        ).status_code == 404
        deposited = client.post(f"/api/vault/deposit/{APPROVAL_ID}", json={"value": SECRET})
    assert deposited.status_code == 201, deposited.text
    receipt = deposited.json()
    assert receipt["set_id"] == VAULT_AUDITED_SET_ID
    assert receipt["key"] == "autonomy:github.token"
    assert receipt["tier"] == "audited"
    assert receipt["name"] == "github.token"
    assert SECRET not in deposited.text
    setting_id = receipt["setting_id"]

    # The row is sealed: the stored payload is a locator, never the value.
    layers = settings_ops.layers_for(VAULT_AUDITED_SET_ID, "autonomy:github.token", org=None)
    assert layers["base"]["id"] == setting_id
    assert SECRET not in json.dumps(layers)

    # A row from another destination is not proof of THIS deposit.
    other = ops.add_setting(
        VAULT_AUDITED_SET_ID, VAULT_CREDENTIAL_REVISION, "autonomy:other.token",
        {"value": "irrelevant"}, org=ops.CALLER_ORG,
    )
    with pytest.raises(ApprovalServiceError, match="invalid_decision"):
        approvals.decide(APPROVAL_ID, _actor(), outcome="granted", decision={"setting_id": other})

    resolution = approvals.decide(
        APPROVAL_ID, _actor(), outcome="granted", decision={"setting_id": setting_id}, now=1002.0,
    )
    assert resolution.payload["decision"] == {
        "setting_id": setting_id, "set_id": VAULT_AUDITED_SET_ID,
        "key": "autonomy:github.token", "tier": "audited",
    }
    assert resolution.payload["result_ref"] == f"vault-seal:{VAULT_AUDITED_SET_ID}/autonomy:github.token"

    # The requester's envelope: value-free terminal result.
    envelope = bridge.envelope(APPROVAL_ID, _org_principal())
    assert envelope["request"] == {**REQUEST, "replace": False}
    assert envelope["result"] == {
        "approved": True,
        "execution": {
            "ok": True, "name": "github.token", "set_id": VAULT_AUDITED_SET_ID,
            "key": "autonomy:github.token", "tier": "audited", "setting_id": setting_id,
        },
    }
    assert SECRET not in json.dumps(envelope)
    # Another session cannot read this requester's envelope.
    with pytest.raises(ApprovalHttpBridgeError, match="not_found"):
        bridge.envelope(APPROVAL_ID, _org_principal(subject="auto-stranger"))

    # The inbox row is resolved; the operator's review carries the same result.
    safe = attention_routes._safe_item(attention_routes._exact_item(APPROVAL_ID))
    assert safe["attention_state"] == "resolved"
    assert central.project_result(approvals.status(APPROVAL_ID))["execution"]["setting_id"] == setting_id

    # The resolution's change event wakes the proven requester once, value-free.
    coordinator.reconcile_exact(APPROVAL_ID)
    assert [w[0] for w in woken] == ["auto-real"]
    spec = woken[0][1]
    assert spec["notification_id"] == f"vault-seal:{APPROVAL_ID}"
    assert spec["status"] == "deposited"
    assert "github.token" in spec["summary"]
    assert "graph vault read github.token" in spec["body"]
    assert SECRET not in json.dumps(spec)

    # Deciding is terminal: a second deposit is refused.
    with _deposit_client(monkeypatch) as client:
        assert client.post(f"/api/vault/deposit/{APPROVAL_ID}", json={"value": "again"}).status_code == 409


def test_deposit_is_refused_for_another_operator_and_a_cold_secured_seal(
    central_runtime, monkeypatch,
):
    approvals, bridge, world = central_runtime
    approval_id = bridge.create(
        central.KIND, _org_principal(), {**REQUEST, "tier": "secured"},
    )
    stranger = HumanApprovalActor._verified(KeyPair.generate().public_hex)
    with _deposit_client(monkeypatch, actor=stranger) as client:
        assert client.post(f"/api/vault/deposit/{approval_id}", json={"value": SECRET}).status_code == 404
    # No personal root is enrolled in this store: the secured seal fails
    # closed with the sealer's message, and nothing is written.
    with _deposit_client(monkeypatch) as client:
        refused = client.post(f"/api/vault/deposit/{approval_id}", json={"value": SECRET})
    assert refused.status_code == 400, refused.text
    assert central.existing_row_id(VAULT_SECURED_SET_ID, "autonomy:github.token") is None
    assert approvals.store.get_resolution(approval_id) is None
    # With the class resolvable the same deposit seals to it.
    monkeypatch.setattr(
        vault_routes, "resolve_personal_root_class_id",
        lambda _selector: world.policy_class.class_id,
    )
    with _deposit_client(monkeypatch) as client:
        sealed = client.post(f"/api/vault/deposit/{approval_id}", json={"value": SECRET})
    assert sealed.status_code == 201, sealed.text
    assert sealed.json()["set_id"] == VAULT_SECURED_SET_ID
    assert central.existing_row_id(VAULT_SECURED_SET_ID, "autonomy:github.token") == sealed.json()["setting_id"]


def test_decline_carries_nothing_and_wakes_with_a_declined_notice(central_runtime):
    approvals, bridge, _world = central_runtime
    woken: list = []
    coordinator = central.VaultSealCoordinator(
        approvals=approvals, notify=lambda session, spec: woken.append((session, spec)) or "accepted",
    )
    approval_id = bridge.create(central.KIND, _org_principal(), dict(REQUEST))
    with pytest.raises(ApprovalServiceError, match="invalid_decision"):
        approvals.decide(approval_id, _actor(), outcome="declined", decision={"setting_id": "x"})
    approvals.decide(approval_id, _actor(), outcome="declined", decision={})
    envelope = bridge.envelope(approval_id, _org_principal())
    assert envelope["result"] == {"approved": False, "outcome": "declined"}
    assert central.project_result(approvals.status(approval_id)) is None
    coordinator.reconcile_exact(approval_id)
    assert woken[0][0] == "auto-real"
    assert woken[0][1]["status"] == "declined"
    assert woken[0][1]["notification_id"] == f"vault-seal:{approval_id}"
    # Other kinds' ids and unknown ids are ignored, never raised on.
    assert coordinator.reconcile_exact("central-unknown-000000000000000000") is None
    assert coordinator.reconcile_exact("not-central") is None


# ── adapter + notification specs ────────────────────────────────────────


def test_http_adapter_maps_legacy_decisions_exactly():
    adapter = central.build_http_adapter()
    assert adapter.legacy_decision_mapper({"approved": False}).outcome == "declined"
    granted = adapter.legacy_decision_mapper({"approved": True, "setting_id": "row-1"})
    assert granted.outcome == "granted" and granted.decision == {"setting_id": "row-1"}
    for body in ({"approved": True}, {"approved": True, "setting_id": "r", "value": "s"}, {"approved": False, "setting_id": "r"}):
        with pytest.raises(ApprovalHttpBridgeError, match="invalid_decision"):
            adapter.legacy_decision_mapper(body)
    with pytest.raises(RuntimeError):
        adapter.request_projector({"request": {}})


def test_notification_specs_never_carry_a_value():
    class _Rec:
        def __init__(self, payload, approval_id="central-x"):
            self.payload = payload
            self.approval_id = approval_id

    def status(outcome, tier):
        return approval_service.ApprovalStatus(
            state="resolved",
            request=_Rec({"request": {"name": "db.password", "tier": tier}, "requester_ref": {}}),
            resolution=_Rec({"outcome": outcome, "resolved_at": 5.0}),
        )

    audited = central.notification(status("granted", "audited"))
    assert audited["status"] == "deposited"
    assert "graph vault read db.password" in audited["body"]
    assert "unattended" in audited["body"]
    secured = central.notification(status("granted", "secured"))
    assert "asks the operator" in secured["body"]
    declined = central.notification(status("declined", "secured"))
    assert declined["status"] == "declined"
    assert central.notification(approval_service.ApprovalStatus(
        state="open", request=_Rec({"request": {}}), resolution=None,
    )) is None


# ── multi-line descriptions and refusal reasons (auto-gf08k) ───────────────

def test_a_multi_line_description_is_accepted_as_one_line(personal_store):
    """Central's approval record keeps review text single-line (no control
    characters), so line breaks are joined rather than the request refused --
    which was a bare `invalid_request`."""
    approvals, _bridge, _runtime = _composition()
    record = approvals.create_from_principal(
        central.KIND, _org_principal(),
        {**REQUEST, "description": "GitHub token for CI\nscopes: contents:read\nrepo: core"})
    joined = "GitHub token for CI · scopes: contents:read · repo: core"
    assert record.payload["safe_review"]["detail"] == joined
    assert record.payload["request"]["description"] == joined


@pytest.mark.parametrize("text", ["one\r\ntwo\r\nthree", "one\rtwo\rthree",
                                  "one\n\n  two\t\tx \nthree\n"])
def test_crlf_cr_blank_lines_and_tabs_fold_the_same_way(personal_store, text):
    approvals, _bridge, _runtime = _composition()
    record = approvals.create_from_principal(
        central.KIND, _org_principal(), {**REQUEST, "description": text})
    assert record.payload["safe_review"]["detail"] in (
        "one · two · three", "one · two x · three")


def test_a_refusal_reaches_the_requester_with_its_reason(personal_store):
    """POST /api/approvals answered a refused vault request with a bare
    {"error": "invalid_request"}; the requester now sees the field and why."""
    from tools.dashboard import approvals_routes
    from tools.dashboard.approval_http_bridge import ApprovalHttpBridgeError
    from tools.dashboard.approval_service import ApprovalServiceError

    approvals, _bridge, _runtime = _composition()
    with pytest.raises(ApprovalServiceError) as info:
        approvals.create_from_principal(
            central.KIND, _org_principal(), {**REQUEST, "name": "bad name"})
    assert info.value.public_detail and "1-128 characters" in info.value.public_detail
    response = approvals_routes._central_error(ApprovalHttpBridgeError(
        "invalid_request", public_detail=info.value.public_detail))
    body = json.loads(response.body)
    assert response.status_code == 400
    assert body == {"error": "invalid_request", "detail": info.value.public_detail}


def test_other_planner_failures_stay_opaque():
    from tools.dashboard import approvals_routes
    from tools.dashboard.approval_http_bridge import ApprovalHttpBridgeError

    body = json.loads(approvals_routes._central_error(
        ApprovalHttpBridgeError("invalid_request")).body)
    assert body == {"error": "invalid_request"}


def test_a_helpers_error_is_not_presented_as_the_requesters_reason(personal_store, monkeypatch):
    """Only the planner's own input checks are written for the requester; a
    helper's ValueError (another kind's wording, a key derivation) stays an
    opaque invalid_request."""
    from tools.dashboard.approval_service import ApprovalServiceError

    def wrong_kind(context):
        raise ValueError("email_send is requested by a session")

    monkeypatch.setattr(central, "_requesting_session", wrong_kind)
    approvals, _bridge, _runtime = _composition()
    with pytest.raises(ApprovalServiceError) as info:
        approvals.create_from_principal(central.KIND, _org_principal(), dict(REQUEST))
    assert info.value.code == "invalid_request" and info.value.public_detail is None
