"""End-to-end secured Setting release through the approval rendezvous."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import (
    api_auth,
    approvals_routes,
    vault_open_approvals,
    vault_release_delivery,
    vault_release_sweeper,
)
from tools.dashboard.dao import approval_requests as ar, vault_releases
from tools.graph import ops, settings_ops
from tools.graph.schemas.vault_credential import (
    VAULT_CREDENTIAL_REVISION,
    VAULT_SECURED_SET_ID,
)
from tools.graph.schemas.personal_identity import (
    PERSONAL_IDENTITY_REVISION,
    PERSONAL_IDENTITY_SET_ID,
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
    revoke_factor,
)
from tools.vault.root_anchor import create_root_anchor
from tools.vault.store import VaultStore
from tools.vault.testkit import make_test_identity


@pytest.fixture
def vault_open_env(tmp_path, monkeypatch):
    graph_db = tmp_path / "personal.db"
    monkeypatch.setenv("GRAPH_DB", str(graph_db))
    monkeypatch.delenv("GRAPH_API", raising=False)
    # ``GRAPH_DB`` is a whole-store pin and the real credential schema is
    # explicitly personal-homed. Make that declared home resolve to the pin,
    # rather than weakening the production conflict guard for this harness.
    from tools.graph import db as graph_db_module
    original_org_db_path = graph_db_module._org_db_path
    monkeypatch.setattr(
        graph_db_module,
        "_org_db_path",
        lambda org, root=None: (
            graph_db if org == "personal"
            else (graph_db.parent / "machine.db") if org == "machine"
            else original_org_db_path(org, root)
        ),
    )
    monkeypatch.setattr(ar, "DB_PATH", tmp_path / "approvals.db")
    release_root = tmp_path / "ramfs"
    (release_root / "auto-real").mkdir(parents=True)
    (release_root / "auto-real").chmod(0o700)
    monkeypatch.setattr(vault_release_delivery, "_delivery_root", lambda: release_root)
    monkeypatch.setattr(
        vault_release_delivery,
        "assert_memory_backed",
        lambda path: None,
    )
    monkeypatch.setattr(
        vault_open_approvals.dashboard_db,
        "get_session",
        lambda name: {
            "tmux_name": name,
            "project": "autonomy-codex",
            "label": "Vault test requester",
        } if name == "auto-real" else None,
    )

    agent = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.ORG_SESSION,
        subject="auto-real",
        org="autonomy",
    )
    operator = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.OPERATOR_COOKIE,
        subject="operator-session",
    )

    def principal(request):
        if request.headers.get("x-test-other-org") == "1":
            return api_auth.ApiPrincipal(
                api_auth.ApiPrincipalKind.ORG_SESSION,
                subject="auto-real",
                org="other-org",
            )
        if request.headers.get("x-test-stranger") == "1":
            return api_auth.ApiPrincipal(
                api_auth.ApiPrincipalKind.ORG_SESSION,
                subject="auto-stranger",
                org="autonomy",
            )
        return operator if request.headers.get("x-test-operator") == "1" else agent

    monkeypatch.setattr(api_auth, "principal_from_request", principal)

    async def no_push(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(approvals_routes.web_push, "register_approval_pending", no_push)
    monkeypatch.setattr(approvals_routes.web_push, "cancel_approval", lambda *_a, **_k: None)
    approvals_routes._executing.clear()
    approvals_routes._decision_waiters.clear()

    world = VaultWorld(tmp_path / "vault").register()
    world.mint_policy_class()
    with VaultStore(graph_db) as store:
        store.put_class(world.policy_class)
        store.put_password_factor(
            world.identity.factor_id,
            world.identity.published.public_key,
            world.identity.armor,
        )
    try:
        yield graph_db, world, TestClient(Starlette(routes=approvals_routes.ROUTES))
    finally:
        world.close()
        clear_seams()
        approvals_routes._executing.clear()
        approvals_routes._decision_waiters.clear()


def test_fake_ssh_key_is_sealed_approved_delivered_and_reclaimed_without_leak(
    vault_open_env,
):
    """The Mac-key rehearsal: full path, exact RAW bytes at the credential's
    own name, session-lifetime file, session-end shred."""
    graph_db, world, client = vault_open_env
    secret = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        + base64.b64encode(secrets.token_bytes(96)).decode("ascii")
        + "\n-----END OPENSSH PRIVATE KEY-----\n"
    )
    expected_digest = hashlib.sha256(secret.encode()).hexdigest()
    setting_id = ops.add_setting(
        VAULT_SECURED_SET_ID,
        VAULT_CREDENTIAL_REVISION,
        "autonomy:test.disposable",
        {"value": secret},
        org=ops.CALLER_ORG,
        vault_policy_class_id=world.policy_class.class_id,
    )
    assert world.identity is not None
    with VaultStore(graph_db) as store:
        store.put_class(world.policy_class)
        store.put_password_factor(
            world.identity.factor_id,
            world.identity.published.public_key,
            world.identity.armor,
        )

    with client:
        created = client.post("/api/approvals", json={
            "kind": "vault_open",
            # A malicious body cannot redirect attribution away from the bearer.
            "session": "auto-spoofed",
            "request": {
                "set_id": VAULT_SECURED_SET_ID,
                "key": "test.disposable",
                "ttl_seconds": 60,
            },
        })
        assert created.status_code == 200, created.text
        rid = created.json()["id"]
        row = ar.get(rid)
        assert row["session"] == "auto-real"
        assert row["request"]["requester"] == {
            "session": "auto-real",
            "organization": "autonomy",
            "workspace": "autonomy-codex",
            "label": "Vault test requester",
        }
        assert row["request"]["setting"]["id"] == setting_id
        assert row["request"]["setting"]["key"] == "autonomy:test.disposable"
        assert row["request"]["target"] == (
            f"{VAULT_SECURED_SET_ID}/autonomy:test.disposable"
        )
        assert secret not in json.dumps(row)

        refused = client.get(f"/api/approvals/{rid}")
        assert refused.status_code == 403
        ceremony = client.get(
            f"/api/approvals/{rid}", headers={"x-test-operator": "1"},
        )
        assert ceremony.status_code == 200, ceremony.text
        bootstrap = ceremony.json()
        assert bootstrap["ceremony"]["policy"] == "password"
        assert bootstrap["ceremony"]["factors"][0]["armor"] == world.identity.armor
        assert "ceremony" not in client.get(
            f"/api/approvals/{rid}?wait=0"
        ).json()
        assert client.get(
            f"/api/approvals/{rid}?wait=0",
            headers={"x-test-stranger": "1"},
        ).status_code == 403

        # The caller supplies a suffix only.  Prefix injection is rejected,
        # and the same suffix under another bearer org cannot cross into the
        # Autonomy row.
        prefixed = client.post("/api/approvals", json={
            "kind": "vault_open",
            "request": {
                "set_id": VAULT_SECURED_SET_ID,
                "key": "other-org:test.disposable",
            },
        })
        assert prefixed.status_code == 400
        assert "unprefixed credential name" in prefixed.text
        other_org = client.post(
            "/api/approvals",
            headers={"x-test-other-org": "1"},
            json={
                "kind": "vault_open",
                "request": {
                    "set_id": VAULT_SECURED_SET_ID,
                    "key": "test.disposable",
                },
            },
        )
        assert other_org.status_code == 400
        assert "other-org:test.disposable" in other_org.text

        # Neither the requesting bearer nor a malformed decline can smuggle
        # opener material into the durable approval result.
        assert client.post(
            f"/api/approvals/{rid}/decision",
            json={"approved": True, "openers": {"pw": "00" * 32}},
        ).status_code == 401
        assert client.post(
            f"/api/approvals/{rid}/decision",
            headers={"x-test-operator": "1"},
            json={"approved": False, "openers": {"pw": "00" * 32}},
        ).status_code == 401
        assert ar.get(rid)["result"] is None

        opener_hex = world.opener_seeds[world.identity.factor_id].hex()
        decided = client.post(
            f"/api/approvals/{rid}/decision",
            headers={"x-test-operator": "1"},
            json={
                "approved": True,
                "openers": {world.identity.factor_id: opener_hex},
            },
        )
        assert decided.status_code == 200, decided.text
        delivered = client.get(f"/api/approvals/{rid}?wait=10").json()
        replay = client.get(f"/api/approvals/{rid}?wait=0").json()

    execution = delivered["result"]["execution"]
    assert execution["ok"] is True
    receipt = execution["receipt"]
    assert receipt["delivery"] == "session-ramfs"
    # The value in its FINAL SHAPE: raw bytes at the credential's bare name
    # (org prefix server-derived, so the consumer path is stable).
    assert receipt["path"] == "/run/secrets/test.disposable"
    assert receipt["lifetime"] == "session"
    assert replay["result"] == delivered["result"]
    assert "value" not in execution
    assert secret not in json.dumps(delivered)
    ramfs_raw = (
        graph_db.parent / "ramfs" / "auto-real" / "test.disposable"
    ).read_text()
    assert hashlib.sha256(ramfs_raw.encode()).hexdigest() == expected_digest
    persisted = ar.get(rid)
    assert "openers" not in json.dumps(persisted)
    assert opener_hex not in json.dumps(persisted)
    assert secret not in json.dumps(persisted)
    assert persisted["result"] == delivered["result"]
    release = vault_releases.get(rid)
    assert release["session"] == "auto-real"
    assert release["container_path"] == receipt["path"]
    assert secret not in json.dumps(release)
    assert "cek" not in json.dumps(delivered).lower()

    host_path = graph_db.parent / "ramfs" / "auto-real" / "test.disposable"
    # Session lifetime: while the session lives, no deadline ever shreds it.
    swept = vault_release_sweeper.sweep(
        session_exists=lambda session: session == "auto-real",
        delivery_root=graph_db.parent / "ramfs",
        now=int(time.time() * 1000) + 10 * 24 * 3600 * 1000,
    )
    assert swept["shredded"] == 0
    assert host_path.exists()
    # Session end is what destroys it.
    ended = vault_release_sweeper.on_session_end(
        "auto-real", delivery_root=graph_db.parent / "ramfs",
    )
    assert ended["shredded"] == 1
    assert not host_path.exists()
    reclaimed = vault_releases.get(rid)
    assert reclaimed["shred_reason"] == "session_end"
    assert secret not in json.dumps(reclaimed)


def test_root_reachable_fake_ssh_key_uses_personal_root_anchor(vault_open_env):
    """One stable anchor, opened by the root ceremony, releases exact bytes."""
    graph_db, world, client = vault_open_env
    root = KeyPair.generate()
    root_password = "disposable-root-password"
    pw_factor, pw_seed = create_password_factor(
        root.public_hex, "pw.test", root_password, iterations=10_000,
    )
    pw_seed[:] = b"\x00" * len(pw_seed)
    envelope = build_envelope(
        root,
        generation=1,
        factors=[pw_factor],
        access=["pw.test"],
        policy=factor_leaf("pw.test"),
    )
    root_armor = emit_armored_envelope(envelope)
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID,
            PERSONAL_IDENTITY_REVISION,
            "default",
            {
                "armored_private_key": root_armor,
                "root_pub": root.public_hex,
                "display_name": "Disposable Operator",
                "created_at": "2026-08-24T00:00:00Z",
            },
            org=None,
        )

    anchor, anchor_seed = create_root_anchor(
        root,
        anchor_id="personal-root-vault",
        display_name="Personal root vault",
        created_at="2026-08-24T00:01:00Z",
    )
    root_class = create_root_reachable_class(
        anchor.published_recipient(),
        display_name="Personal root vault",
        created_at="2026-08-24T00:02:00Z",
    )
    world.policy_class = root_class
    with VaultStore(graph_db) as store:
        store.put_root_anchor(anchor)
        store.put_class(root_class)
    secret = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        + base64.b64encode(secrets.token_bytes(96)).decode("ascii")
        + "\n-----END OPENSSH PRIVATE KEY-----\n"
    )
    expected_digest = hashlib.sha256(secret.encode()).hexdigest()
    ops.add_setting(
        VAULT_SECURED_SET_ID,
        VAULT_CREDENTIAL_REVISION,
        "autonomy:test.root-reachable-ssh",
        {"value": secret},
        org=ops.CALLER_ORG,
        vault_policy_class_id=root_class.class_id,
    )
    with client:
        created = client.post("/api/approvals", json={
            "kind": "vault_open",
            "request": {
                "set_id": VAULT_SECURED_SET_ID,
                "key": "test.root-reachable-ssh",
                "ttl_seconds": 60,
            },
        })
        assert created.status_code == 200, created.text
        rid = created.json()["id"]
        review = client.get(
            f"/api/approvals/{rid}", headers={"x-test-operator": "1"},
        )
        assert review.status_code == 200, review.text
        ceremony = review.json()["ceremony"]
        assert ceremony["v"] == 2
        assert ceremony["governance"] == root_class.governance
        assert ceremony["anchor"] == anchor.to_dict()
        assert ceremony["root"]["armor"] == root_armor
        assert ceremony["root"]["armor_version"] == 3
        assert ceremony["root"]["root_pub"] == root.public_hex
        assert ceremony["root"]["methods"] == ["password"]

        decided = client.post(
            f"/api/approvals/{rid}/decision",
            headers={"x-test-operator": "1"},
            json={
                "approved": True,
                "openers": {anchor.anchor_id: anchor_seed.hex()},
            },
        )
        assert decided.status_code == 200, decided.text
        delivered = client.get(f"/api/approvals/{rid}?wait=10").json()

    receipt = delivered["result"]["execution"]["receipt"]
    ramfs_raw = (
        graph_db.parent / "ramfs" / "auto-real" / "test.root-reachable-ssh"
    ).read_text()
    assert hashlib.sha256(ramfs_raw.encode()).hexdigest() == expected_digest
    assert secret not in json.dumps(delivered)
    assert secret not in json.dumps(ar.get(rid))
    assert anchor_seed.hex() not in json.dumps(ar.get(rid))
    assert receipt["path"] == "/run/secrets/test.root-reachable-ssh"


def test_vault_open_refuses_setting_drift(vault_open_env):
    graph_db, world, client = vault_open_env
    setting_id = ops.add_setting(
        VAULT_SECURED_SET_ID,
        VAULT_CREDENTIAL_REVISION,
        "autonomy:test.drift",
        {"value": "first"},
        org=ops.CALLER_ORG,
        vault_policy_class_id=world.policy_class.class_id,
    )
    with VaultStore(graph_db) as store:
        store.put_class(world.policy_class)
        store.put_password_factor(
            world.identity.factor_id,
            world.identity.published.public_key,
            world.identity.armor,
        )
    with client:
        rid = client.post("/api/approvals", json={
            "kind": "vault_open",
            "request": {
                "set_id": VAULT_SECURED_SET_ID,
                "key": "test.drift",
            },
        }).json()["id"]
        settings_ops.override_setting(
            setting_id,
            {"value": "second"},
            org=None,
            vault_policy_class_id=world.policy_class.class_id,
        )
        client.post(
            f"/api/approvals/{rid}/decision",
            headers={"x-test-operator": "1"},
            json={
                "approved": True,
                "openers": {
                    world.identity.factor_id:
                        world.opener_seeds[world.identity.factor_id].hex(),
                },
            },
        )
        result = client.get(f"/api/approvals/{rid}?wait=10").json()["result"]
    assert result["execution"]["ok"] is False
    assert "changed before approval" in result["execution"]["error"]
    assert "value" not in result["execution"]


def test_vault_open_uses_the_generation_named_by_an_older_setting(vault_open_env):
    """A later revocation must not substitute today's wraps for old ciphertext."""
    graph_db, world, client = vault_open_env
    ops.add_setting(
        VAULT_SECURED_SET_ID,
        VAULT_CREDENTIAL_REVISION,
        "autonomy:test.older-generation",
        {"value": "sealed-before-revocation"},
        org=ops.CALLER_ORG,
        vault_policy_class_id=world.policy_class.class_id,
    )
    original = world.policy_class
    old_generation_id = original.current().gen_id
    survivor = make_test_identity()
    extended = extend_class(original, world.opener_seeds, survivor.published)
    current = revoke_factor(
        extended, world.identity.factor_id, created_at="2026-08-24T00:00:00Z",
    )
    assert current.current().gen_id != old_generation_id
    with VaultStore(graph_db) as store:
        store.put_class(current)
        store.put_password_factor(
            world.identity.factor_id,
            world.identity.published.public_key,
            world.identity.armor,
        )
        store.put_password_factor(
            survivor.factor_id,
            survivor.published.public_key,
            survivor.armor,
        )

    with client:
        created = client.post("/api/approvals", json={
            "kind": "vault_open",
            "request": {
                "set_id": VAULT_SECURED_SET_ID,
                "key": "test.older-generation",
            },
        })
        assert created.status_code == 200, created.text
        rid = created.json()["id"]
        staged = ar.get(rid)["staged"]
        assert staged["class_snapshot"]["generation"]["gen_id"] == old_generation_id
        review = client.get(
            f"/api/approvals/{rid}", headers={"x-test-operator": "1"},
        ).json()

    assert {factor["factor_id"] for factor in review["ceremony"]["factors"]} == {
        world.identity.factor_id,
        survivor.factor_id,
    }
