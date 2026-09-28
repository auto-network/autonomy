"""vault_open as a Settings-native Central approval (auto-fkhq0.27).

Against the real ApprovalService (in-memory store) and a real vault (sealed
Setting, policy class, the operator's content key computed exactly as the
browser does), this proves:

- the request is frozen under the proven requesting session; the staged
  context holds identifiers and digests only; the decision carries nothing;
- the operator's bootstrap is the frozen ceremony and open bundle;
- delivery is a separate operator step bound to the canonical Grant: the right
  content key writes the exact bytes to the requester's ramfs once; a wrong key
  opens nothing and can be retried; a replay returns the same receipt;
- it refuses a pending, declined, foreign-machine, drifted, or out-of-window
  release, with fixed codes, and the content key never appears in a Central
  row, the lease, a response, an error or a log line;
- the requester's result is null until the receipt exists;
- the production composition claims the kind and mounts the two routes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from types import SimpleNamespace

import pytest

from tools.dashboard import (
    api_auth,
    vault_open_approvals,
    vault_open_central as central,
    vault_release_delivery,
)
from tools.dashboard.approval_kind_registry import build_production_registry
from tools.dashboard.approval_service import (
    ApprovalService,
    HumanApprovalActor,
    InMemoryApprovalStore,
)
from tools.dashboard.dao import vault_releases
from tools.graph import ops, settings_ops
from tools.graph.schemas.central_attention import ApprovalRequestV1
from tools.graph.schemas.personal_identity import (
    PERSONAL_IDENTITY_REVISION,
    PERSONAL_IDENTITY_SET_ID,
)
from tools.graph.schemas.vault_credential import (
    VAULT_CREDENTIAL_REVISION,
    VAULT_SECURED_SET_ID,
)
from tools.graph.tests.vault_read_harness import VaultWorld, clear_seams
from tools.network.idkit import KeyPair
from tools.network.idkit.root_factor_policy import (
    build_envelope,
    create_password_factor,
    emit_armored_envelope,
    factor_leaf,
)
from tools.vault.policy_class import (
    create_root_reachable_class,
    extend_class,
    open_cek,
    revoke_factor,
)
from tools.vault.root_anchor import create_root_anchor
from tools.vault.store import VaultStore
from tools.vault.testkit import make_test_identity

ROOT = KeyPair.generate()
HERE = "h" * 43
ELSEWHERE = "e" * 43
NOW = 10_000.0


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Index:
    """The Central item lookup the delivery endpoint resolves through."""

    def __init__(self):
        self.items: dict[str, str] = {}

    def track(self, approval_id: str) -> str:
        aid = central.vault_open_attention_id(approval_id)
        self.items[aid] = approval_id
        return aid

    def get_query_item(self, attention_id):
        if not isinstance(attention_id, str) or not attention_id.startswith("attention-"):
            raise ValueError("bad id")
        approval_id = self.items.get(attention_id)
        if approval_id is None:
            return None
        return SimpleNamespace(attention_id=attention_id, payload={"object_ref": approval_id})


@pytest.fixture
def env(tmp_path, monkeypatch):
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
    delivered: dict = {}

    def fake_deliver(container, filename, data, **_kw):
        delivered[(container, filename)] = data
        return f"/run/secrets/{filename}"

    monkeypatch.setattr(vault_release_delivery, "deliver_secret_file", fake_deliver)
    sessions = {"auto-real": {"tmux_name": "auto-real", "project": "autonomy-codex",
                              "label": "Vault test requester"}}
    monkeypatch.setattr(vault_open_approvals.dashboard_db, "get_session", sessions.get)

    world = VaultWorld(tmp_path / "vault").register()
    world.mint_policy_class()
    with VaultStore(graph_db) as store:
        store.put_class(world.policy_class)
        store.put_password_factor(world.identity.factor_id,
                                  world.identity.published.public_key, world.identity.armor)
    clock = Clock()
    # Leases live in the worker's shared machine store: ids unique per test.
    run = secrets.token_hex(4)
    ids = iter(f"central-vault-{run}-{n:04d}" for n in range(1, 99))
    approvals = ApprovalService(
        registry=build_production_registry(runtimes={
            central.KIND: central.build_approval_runtime(
                destination_resolver=lambda: HERE, machine_label=lambda: "Home"),
        }),
        store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: ROOT.public_hex,
        session_label_resolver=lambda subject: f"{subject} · Signing a release",
        clock=clock,
        id_factory=lambda: next(ids),
    )
    index = Index()
    live = {"auto-real": True}
    notes: list = []
    delivery = central.VaultOpenDelivery(
        approvals=approvals, index=index, destination_resolver=lambda: HERE,
        session_live=lambda s: live.get(s, False), clock=clock,
        notify=lambda session, approval_id, **spec: notes.append((session, approval_id, spec)),
    )
    try:
        yield SimpleNamespace(graph_db=graph_db, world=world, approvals=approvals, index=index,
                              delivery=delivery, clock=clock, delivered=delivered,
                              sessions=sessions, live=live, notes=notes)
    finally:
        world.close()
        clear_seams()


def _agent(subject="auto-real", org="autonomy"):
    return api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.ORG_SESSION, subject=subject, org=org)


def _actor():
    return HumanApprovalActor._verified(ROOT.public_hex)


def _seal(env, key="test.disposable", value=None):
    value = value or ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
                      + base64.b64encode(secrets.token_bytes(96)).decode("ascii")
                      + "\n-----END OPENSSH PRIVATE KEY-----\n")
    setting_id = ops.add_setting(
        VAULT_SECURED_SET_ID, VAULT_CREDENTIAL_REVISION, f"autonomy:{key}",
        {"value": value}, org=ops.CALLER_ORG,
        vault_policy_class_id=env.world.policy_class.class_id,
    )
    return setting_id, value


def _request(env, key="test.disposable", ttl=60, principal=None):
    record = env.approvals.create_from_principal(
        central.KIND, principal or _agent(),
        {"set_id": VAULT_SECURED_SET_ID, "key": key, "ttl_seconds": ttl},
    )
    return record.approval_id, env.index.track(record.approval_id)


def _content_key(env, bundle, seeds=None, policy_class=None):
    return open_cek(
        policy_class or env.world.policy_class, seeds or env.world.opener_seeds,
        bundle["sealed_cek"], genesis_id=bundle["genesis_id"],
        setting_name=bundle["setting_name"], required_policy=bundle["policy"],
    ).hex()


def _grant(env, approval_id):
    env.approvals.decide(approval_id, _actor(), outcome="granted", decision={})


def _code(fn, *args):
    with pytest.raises(central.VaultOpenDeliveryError) as exc:
        fn(*args)
    return exc.value.code


# ── the full path ──────────────────────────────────────────────────────────

def test_a_granted_release_is_delivered_once_with_the_operators_key(env, caplog):
    caplog.set_level(logging.DEBUG)
    setting_id, value = _seal(env)
    approval_id, aid = _request(env)
    status = env.approvals.status(approval_id)
    payload = status.request.payload
    ApprovalRequestV1.validate(payload)
    request = payload["request"]
    assert request["requester"] == {"session": "auto-real", "organization": "autonomy",
                                    "workspace": "autonomy-codex",
                                    "label": "Vault test requester"}
    assert request["setting"] == {"set_id": VAULT_SECURED_SET_ID,
                                  "key": "autonomy:test.disposable", "id": setting_id}
    assert payload["safe_review"]["machine_label"] == "Home"
    staged = payload["staged"]
    assert set(staged) == {"v", "org", "setting_id", "sealed_digest", "class_id", "gen_id",
                           "class_digest", "factor_digest", "result_destination_id",
                           "machine_label"}
    assert env.delivery.state(status) == central.PENDING
    assert env.delivery.requester_result(status) is None

    boot = env.delivery.bootstrap(aid)
    assert boot["ceremony"]["policy"] == "password"
    assert boot["ceremony"]["factors"][0]["armor"] == env.world.identity.armor
    assert boot["bundle"]["class_id"] == env.world.policy_class.class_id
    key = _content_key(env, boot["bundle"])

    # Before the Grant, a key opens nothing.
    assert _code(env.delivery.deliver, aid, {"content_key": key}) == "not_actionable"
    _grant(env, approval_id)
    status = env.approvals.status(approval_id)
    assert status.resolution.payload["decision"] == {}
    assert env.delivery.state(status) == central.AWAITING
    assert env.delivery.requester_result(status) is None  # null until the receipt

    done = env.delivery.deliver(aid, {"content_key": key})
    receipt = done["receipt"]
    assert receipt == {"release_id": approval_id, "delivery": "session-ramfs",
                       "path": "/run/secrets/test.disposable", "ttl_seconds": 60}
    assert env.delivered[("auto-real", "test.disposable")].decode() == value
    status = env.approvals.status(approval_id)
    assert env.delivery.state(status) == central.DELIVERED
    assert env.delivery.requester_result(status) == {
        "approved": True, "execution": {"ok": True, "receipt": receipt}}
    assert env.delivery.operator_result(status)["path"] == "/run/secrets/test.disposable"
    assert env.notes[-1][0:2] == ("auto-real", approval_id)
    assert env.notes[-1][2]["status"] == "released"

    # A replay returns the same receipt and writes nothing again.
    env.delivered.clear()
    assert env.delivery.deliver(aid, {"content_key": key}) == done
    assert env.delivered == {}

    # The key and the value are nowhere: Central rows, lease, receipt, logs.
    scanned = json.dumps([payload, status.resolution.payload, vault_releases.get(approval_id),
                          done, env.notes])
    for needle in (key, value, env.world.opener_seeds[env.world.identity.factor_id].hex()):
        assert needle not in scanned
        assert needle not in caplog.text


def test_a_wrong_key_opens_nothing_and_the_right_one_still_works(env, caplog):
    caplog.set_level(logging.DEBUG)
    _seal(env)
    approval_id, aid = _request(env)
    key = _content_key(env, env.delivery.bootstrap(aid)["bundle"])
    _grant(env, approval_id)
    wrong = secrets.token_hex(32)
    assert _code(env.delivery.deliver, aid, {"content_key": wrong}) == "open_failed"
    assert vault_releases.get(approval_id) is None
    assert env.delivered == {}
    assert env.delivery.deliver(aid, {"content_key": key})["receipt"]["path"]
    assert wrong not in caplog.text and key not in caplog.text


def test_the_root_reachable_anchor_releases_exact_bytes(env):
    root = KeyPair.generate()
    pw_factor, pw_seed = create_password_factor(root.public_hex, "pw.test",
                                                "disposable-root-password", iterations=10_000)
    pw_seed[:] = b"\x00" * len(pw_seed)
    envelope = build_envelope(root, generation=1, factors=[pw_factor], access=["pw.test"],
                              policy=factor_leaf("pw.test"))
    root_armor = emit_armored_envelope(envelope)
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID, PERSONAL_IDENTITY_REVISION, "default",
            {"armored_private_key": root_armor, "root_pub": root.public_hex,
             "display_name": "Disposable Operator", "created_at": "2026-08-24T00:00:00Z"},
            org=None,
        )
    anchor, anchor_seed = create_root_anchor(root, anchor_id="personal-root-vault",
                                             display_name="Personal root vault",
                                             created_at="2026-08-24T00:01:00Z")
    root_class = create_root_reachable_class(anchor.published_recipient(),
                                             display_name="Personal root vault",
                                             created_at="2026-08-24T00:02:00Z")
    env.world.policy_class = root_class
    with VaultStore(env.graph_db) as store:
        store.put_root_anchor(anchor)
        store.put_class(root_class)
    _, value = _seal(env, key="test.root-reachable-ssh")
    approval_id, aid = _request(env, key="test.root-reachable-ssh")
    boot = env.delivery.bootstrap(aid)
    assert boot["ceremony"]["v"] == 2
    assert boot["ceremony"]["root"]["armor"] == root_armor
    assert boot["ceremony"]["root"]["methods"] == ["password"]
    key = _content_key(env, boot["bundle"], seeds={anchor.anchor_id: anchor_seed},
                       policy_class=root_class)
    _grant(env, approval_id)
    env.delivery.deliver(aid, {"content_key": key})
    assert env.delivered[("auto-real", "test.root-reachable-ssh")].decode() == value
    assert anchor_seed.hex() not in json.dumps(env.approvals.status(approval_id).request.payload)


def test_the_generation_named_by_an_older_setting_is_the_one_served(env):
    """A later revocation must not substitute today's wraps for old ciphertext."""
    _seal(env, key="test.older-generation", value="sealed-before-revocation")
    original = env.world.policy_class
    old_generation = original.current().gen_id
    survivor = make_test_identity()
    current = revoke_factor(extend_class(original, env.world.opener_seeds, survivor.published),
                            env.world.identity.factor_id, created_at="2026-08-24T00:00:00Z")
    with VaultStore(env.graph_db) as store:
        store.put_class(current)
        store.put_password_factor(survivor.factor_id, survivor.published.public_key,
                                  survivor.armor)
    approval_id, aid = _request(env, key="test.older-generation")
    assert env.approvals.status(approval_id).request.payload["staged"]["gen_id"] == old_generation
    factors = {f["factor_id"] for f in env.delivery.bootstrap(aid)["ceremony"]["factors"]}
    assert factors == {env.world.identity.factor_id, survivor.factor_id}


# ── refusals ───────────────────────────────────────────────────────────────

def test_the_request_names_nothing_the_bearer_does_not_prove(env):
    _seal(env)
    with pytest.raises(Exception):  # a server-derived prefix cannot be injected
        env.approvals.create_from_principal(
            central.KIND, _agent(),
            {"set_id": VAULT_SECURED_SET_ID, "key": "other-org:test.disposable"})
    with pytest.raises(Exception):  # the same suffix under another org cannot cross
        env.approvals.create_from_principal(
            central.KIND, _agent(org="other-org"),
            {"set_id": VAULT_SECURED_SET_ID, "key": "test.disposable"})
    with pytest.raises(Exception):  # no field outside set_id / key / ttl_seconds
        env.approvals.create_from_principal(
            central.KIND, _agent(),
            {"set_id": VAULT_SECURED_SET_ID, "key": "test.disposable", "session": "x"})
    with pytest.raises(Exception):  # a session without a launcher record
        env.approvals.create_from_principal(
            central.KIND, _agent(subject="auto-stranger"),
            {"set_id": VAULT_SECURED_SET_ID, "key": "test.disposable"})


def test_a_decision_carrying_a_key_is_refused(env):
    _seal(env)
    approval_id, _ = _request(env)
    with pytest.raises(Exception):
        env.approvals.decide(approval_id, _actor(), outcome="granted",
                             decision={"content_key": "00" * 32})
    assert env.approvals.status(approval_id).resolution is None


@pytest.mark.parametrize("body", [
    {}, {"content_key": "00" * 31}, {"content_key": "ZZ" * 32}, {"content_key": "00" * 32, "x": 1},
    {"approved": True, "content_key": "00" * 32}, ["00" * 32], None,
])
def test_the_delivery_body_is_exactly_one_content_key(env, body):
    _seal(env)
    approval_id, aid = _request(env)
    _grant(env, approval_id)
    assert _code(env.delivery.deliver, aid, body) == "invalid_request"


def test_a_declined_release_delivers_nothing_and_wakes_the_requester(env):
    _seal(env)
    approval_id, aid = _request(env)
    key = _content_key(env, env.delivery.bootstrap(aid)["bundle"])
    env.approvals.decide(approval_id, _actor(), outcome="declined", decision={})
    assert _code(env.delivery.deliver, aid, {"content_key": key}) == "not_actionable"
    assert _code(env.delivery.bootstrap, aid) == "not_actionable"
    assert env.delivered == {}
    coordinator = central.VaultOpenCoordinator(
        delivery=env.delivery, approvals=env.approvals,
        index=SimpleNamespace(publish=lambda producer, status: None), producer=None)
    coordinator.reconcile_exact(approval_id)
    assert env.notes[-1][2]["status"] == "declined"


def test_another_machine_neither_bootstraps_nor_delivers(env):
    _seal(env)
    approval_id, aid = _request(env)
    key = _content_key(env, env.delivery.bootstrap(aid)["bundle"])
    _grant(env, approval_id)
    other = central.VaultOpenDelivery(
        approvals=env.approvals, index=env.index, destination_resolver=lambda: ELSEWHERE,
        session_live=lambda s: True, clock=env.clock, notify=lambda *a, **k: None)
    assert other.state(env.approvals.status(approval_id)) == central.ELSEWHERE
    assert _code(other.bootstrap, aid) == "elsewhere"
    assert _code(other.deliver, aid, {"content_key": key}) == "elsewhere"
    assert env.delivered == {}


def test_the_window_closes_at_thirty_minutes_or_when_the_session_ends(env):
    _seal(env)
    approval_id, aid = _request(env)
    key = _content_key(env, env.delivery.bootstrap(aid)["bundle"])
    _grant(env, approval_id)
    env.clock.t = NOW + central.DELIVERY_WINDOW_SECONDS
    status = env.approvals.status(approval_id)
    assert env.delivery.state(status) == central.EXPIRED
    assert env.delivery.requester_result(status)["execution"]["ok"] is False
    assert _code(env.delivery.deliver, aid, {"content_key": key}) == "window_closed"
    env.clock.t = NOW + 1
    env.live["auto-real"] = False
    assert _code(env.delivery.deliver, aid, {"content_key": key}) == "window_closed"
    assert env.delivered == {}


def test_a_drifted_setting_is_refused_before_the_key_touches_it(env):
    setting_id, _ = _seal(env, key="test.drift", value="first")
    approval_id, aid = _request(env, key="test.drift")
    key = _content_key(env, env.delivery.bootstrap(aid)["bundle"])
    _grant(env, approval_id)
    settings_ops.override_setting(setting_id, {"value": "second"}, org=None,
                                  vault_policy_class_id=env.world.policy_class.class_id)
    assert _code(env.delivery.deliver, aid, {"content_key": key}) in {"binding_drift", "open_failed"}
    assert env.delivered == {}


def test_a_moved_session_is_binding_drift(env):
    _seal(env)
    approval_id, aid = _request(env)
    key = _content_key(env, env.delivery.bootstrap(aid)["bundle"])
    _grant(env, approval_id)
    env.sessions["auto-real"] = {**env.sessions["auto-real"], "project": "another-workspace"}
    assert _code(env.delivery.deliver, aid, {"content_key": key}) == "binding_drift"


def test_a_failed_ramfs_write_is_terminal_and_logs_no_key(env, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _seal(env)
    approval_id, aid = _request(env)
    key = _content_key(env, env.delivery.bootstrap(aid)["bundle"])
    _grant(env, approval_id)

    def broken(container, filename, data, **_kw):
        raise OSError(f"ramfs refused {data!r}")

    monkeypatch.setattr(vault_release_delivery, "deliver_secret_file", broken)
    assert _code(env.delivery.deliver, aid, {"content_key": key}) == "delivery_failed"
    status = env.approvals.status(approval_id)
    assert env.delivery.state(status) == central.FAILED
    assert env.delivery.requester_result(status)["execution"]["ok"] is False
    assert _code(env.delivery.deliver, aid, {"content_key": key}) == "delivery_failed"
    assert key not in caplog.text


def test_an_unknown_item_is_not_found(env):
    assert _code(env.delivery.bootstrap, "attention-nope") == "not_found"
    assert _code(env.delivery.deliver, "not-an-id", {"content_key": "00" * 32}) == "not_found"


# ── composition and routes ─────────────────────────────────────────────────

def test_production_composition_claims_vault_open_and_mounts_the_routes():
    from tools.dashboard import attention_routes

    runtime = attention_routes.build_production_runtime()
    assert runtime.approval_http.claims_kind(central.KIND)
    assert runtime.approval_http.migrated_kind(central.KIND)
    assert isinstance(runtime.vault_open_delivery, central.VaultOpenDelivery)
    assert central.KIND in runtime.operator_result_projectors
    paths = {route.path for route in attention_routes.routes}
    assert "/api/attention/items/{attention_id:path}/vault-open-bootstrap" in paths
    assert "/api/attention/items/{attention_id:path}/vault-open-delivery" in paths


def test_the_legacy_vault_open_hooks_are_gone():
    from tools.dashboard import approvals_routes

    for name in ("PREPARE_CREATE_FROM_REQUEST", "AUTHORIZE_GET", "ENRICH_FROM_REQUEST",
                 "RESULT_BUILDERS", "WAIT_RESULT_BUILDERS", "SESSION_NOTIFIERS"):
        assert not hasattr(approvals_routes, name), name
    assert central.KIND not in approvals_routes.AUTHORIZE_DECISION
    assert central.KIND not in approvals_routes.EXECUTORS


def test_the_delivery_route_never_echoes_the_body(monkeypatch):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from tools.dashboard import attention_routes

    key = "ab" * 32

    class Refusing:
        def deliver(self, attention_id, body):
            raise central.VaultOpenDeliveryError("open_failed")

    monkeypatch.setattr(attention_routes, "operator_mutation_guard", lambda request: None)
    runtime = attention_routes.build_production_runtime()
    previous = attention_routes.configure_runtime(
        attention_routes.AttentionRouteRuntime(
            index=runtime.index, presentation=runtime.presentation, approvals=runtime.approvals,
            hub=runtime.hub, approval_http=runtime.approval_http,
            vault_open_delivery=Refusing()))
    try:
        client = TestClient(Starlette(routes=attention_routes.routes))
        resp = client.post("/api/attention/items/attention-x/vault-open-delivery",
                           json={"content_key": key})
        assert resp.status_code == 409
        assert resp.json() == {"error": "open_failed"}
        assert key not in resp.text
    finally:
        attention_routes.configure_runtime(previous)
