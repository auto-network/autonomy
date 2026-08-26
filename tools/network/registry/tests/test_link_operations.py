from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tools.network.idkit import KeyPair, canonical_json
from tools.network.registry.app import create_app
from tools.network.registry import relay
from tools.network.registry.signing import sign_request
from tools.network.registry.store import LinkGrant, LinkOperation, RegistryStore

from .conftest import DAY, ORG, TARGET, register, signed


def _digest(value) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _receipt_material(app, operation: str = "publish"):
    binding = app.state.store.get_org(ORG)
    operation_id = hashlib.sha256(f"operation:{operation}".encode()).hexdigest()
    proof = hashlib.sha256(f"proof:{operation}".encode()).digest()
    proof_wire = base64.urlsafe_b64encode(proof).decode().rstrip("=")
    commitment = hashlib.sha256(
        b"autonomy.link.origin-proof-commitment.v1\n" + proof
    ).hexdigest()
    if operation == "publish":
        registry_input = {
            "operation_id": operation_id,
            "target_uuid": TARGET,
            "target_type": "present",
            "meta": {"ttl": 3600, "label": "Launch review"},
        }
        operand = None
        operand_digest = None
    else:
        registry_input = {"operation_id": operation_id}
        operand = "12" * 16
        operand_digest = _digest(["autonomy.link.operand", 1, "revoke", operand])
    payload = {
        "org_uuid": ORG,
        "operation": operation,
        "operation_id": operation_id,
        "binding_root_pub": binding.root_pub,
        "binding_generation": binding.binding_generation,
        "registry_input_digest": _digest(registry_input),
        "local_intent_digest": "ab" * 32,
        "origin_proof_commitment": commitment,
    }
    if operation == "publish":
        payload["target_type"] = registry_input["target_type"]
        payload["registry_input"] = registry_input
    if operand_digest is not None:
        payload["operand_digest"] = operand_digest
    return payload, registry_input, proof_wire, operand


def test_existing_registry_database_backfills_one_stable_generation(tmp_path):
    path = tmp_path / "registry.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE orgs (org_uuid TEXT PRIMARY KEY, root_pub TEXT NOT NULL, "
        "recovery_policy TEXT NOT NULL, recovery_pub TEXT, created_at INTEGER NOT NULL, "
        "expires_at INTEGER NOT NULL, renewed_at INTEGER, endpoint_hints TEXT, "
        "policy_epoch INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE links (token TEXT PRIMARY KEY, org_uuid TEXT NOT NULL, "
        "target_uuid TEXT NOT NULL, target_type TEXT NOT NULL, meta TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, expires_at INTEGER, revoked_at INTEGER, "
        "signer_pub TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO orgs VALUES (?, ?, 'none', NULL, 1, 9999999999, NULL, NULL, 0)",
        (ORG, "ab" * 32),
    )
    other_org = "77777777-7777-4777-8777-777777777777"
    conn.execute(
        "INSERT INTO orgs VALUES (?, ?, 'none', NULL, 1, 9999999999, NULL, NULL, 0)",
        (other_org, "cd" * 32),
    )
    conn.commit()
    conn.close()

    first_store = RegistryStore(str(path))
    generation = first_store.get_org(ORG).binding_generation
    other_generation = first_store.get_org(other_org).binding_generation
    assert len(generation) == 64
    assert len(other_generation) == 64
    assert other_generation != generation
    int(generation, 16)
    first_store.close()
    second_store = RegistryStore(str(path))
    assert second_store.get_org(ORG).binding_generation == generation
    assert second_store.get_org(other_org).binding_generation == other_generation
    second_store.close()


def test_binding_generation_and_closed_policy_are_authoritative(
    client, app, clock, root, recovery
):
    first = register(
        client, clock, root, policy="recovery-key", recovery_pub=recovery.public_hex
    )
    assert first.status_code == 201
    body = first.json()
    generation = body["binding_generation"]
    assert body == {
        "outcome": "claimed",
        "org_uuid": ORG,
        "root_pub": root.public_hex,
        "binding_generation": generation,
        "expires_at": clock.now + 30 * DAY,
        "recovery_policy": {
            "mode": "recovery-key",
            "recovery_pub": recovery.public_hex,
        },
    }

    retry = register(
        client, clock, root, policy="recovery-key", recovery_pub=recovery.public_hex
    )
    assert retry.status_code == 201
    assert retry.json()["outcome"] == "already_bound_self"
    assert retry.json()["binding_generation"] == generation
    assert retry.json()["recovery_policy"] == body["recovery_policy"]

    renewed = signed(
        client,
        "POST",
        f"/v1/orgs/{ORG}/renew",
        root,
        {},
        clock,
        expect=200,
    ).json()
    assert renewed["binding_generation"] == generation
    assert renewed["root_pub"] == root.public_hex
    assert renewed["recovery_policy"] == body["recovery_policy"]

    clock.advance(31 * DAY)
    reclaimed = register(
        client, clock, root, policy="recovery-key", recovery_pub=recovery.public_hex
    )
    assert reclaimed.status_code == 201
    assert reclaimed.json()["outcome"] == "reclaimed_expired"
    assert reclaimed.json()["binding_generation"] != generation


def test_two_store_connections_serialize_claim_and_first_execution(tmp_path):
    path = str(tmp_path / "shared-registry.db")
    first_store = RegistryStore(path)
    first_store.create_org(
        ORG,
        "ab" * 32,
        "none",
        None,
        now=100,
        expires_at=10_000,
    )
    second_store = RegistryStore(path)
    binding = first_store.get_org(ORG)
    operation = LinkOperation(
        org_uuid=ORG,
        operation_id="11" * 32,
        operation="publish",
        binding_root_pub=binding.root_pub,
        binding_generation=binding.binding_generation,
        receipt_request_digest="22" * 32,
        acceptance_envelope_digest="33" * 32,
        registry_input_digest="44" * 32,
        local_intent_digest="55" * 32,
        operand_digest=None,
        origin_proof_commitment="66" * 32,
        source_expires_at_ms=None,
        accepted_at=101,
        signer_pub="77" * 32,
        subject_kind="operator",
        subject_id="op-1",
        receipt_json="{}",
        receipt_signature="88" * 64,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda store: store.claim_link_operation(
                    operation, now=101, trusted_now_ms=lambda: 101_000
                )[0],
                (first_store, second_store),
            )
        )
    assert sorted(claims) == ["created", "replay"]

    def execute(store: RegistryStore, token: str):
        return store.execute_publish_operation(
            ORG,
            operation.operation_id,
            LinkGrant(
                token=token,
                org_uuid=ORG,
                target_uuid=TARGET,
                target_type="present",
                meta={},
                created_at=102,
                expires_at=None,
                revoked_at=None,
                signer_pub=operation.signer_pub,
                subject_kind=operation.subject_kind,
                subject_id=operation.subject_id,
                operation_id=operation.operation_id,
            ),
            now=102,
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        executions = list(
            pool.map(
                lambda pair: execute(*pair),
                ((first_store, "aa" * 16), (second_store, "bb" * 16)),
            )
        )
    assert sorted(executions) == ["created", "replay"]
    completed = first_store.get_link_operation(ORG, operation.operation_id)
    assert completed.state == "succeeded"
    assert first_store.get_link(completed.result_token).operation_id == operation.operation_id
    first_store.close()
    second_store.close()


def test_publish_receipt_reply_loss_and_execution_replay(
    client, app, clock, root, session_key, session_cert
):
    assert register(client, clock, root).status_code == 201
    payload, registry_input, proof, _ = _receipt_material(app, "publish")
    first = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        session_key,
        payload,
        clock,
        cert=session_cert,
        expect=201,
    )
    assert first.headers["cache-control"] == "no-store"
    first = first.json()
    assert "registry_input" not in first["receipt"]
    assert "Launch review" not in app.state.store.get_link_operation(
        ORG, payload["operation_id"]
    ).receipt_json
    accepted_at = first["receipt"]["accepted_at"]
    accepting_signer = first["receipt"]["accepting_signer_pub"]
    root_retrieval = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        root,
        payload,
        clock,
        expect=201,
    ).json()
    assert root_retrieval == first
    assert root_retrieval["receipt"]["accepting_signer_pub"] == accepting_signer
    clock.advance(1)
    replay = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        session_key,
        payload,
        clock,
        cert=session_cert,
        expect=201,
    ).json()
    assert replay == first
    assert replay["receipt"]["accepted_at"] == accepted_at

    execution = {
        "receipt": first["receipt"],
        "signature": first["signature"],
        "origin_proof": proof,
        "registry_input": registry_input,
    }
    path = f"/v1/link-operations/{payload['operation_id']}/execute"
    wrong_proof = dict(execution)
    wrong_proof["origin_proof"] = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    assert client.post(path, json=wrong_proof).status_code == 403
    created = client.post(path, json=execution)
    assert created.status_code == 200, created.json()
    assert created.headers["cache-control"] == "no-store"
    again = client.post(path, json=execution)
    assert again.status_code == 200, again.json()
    assert again.json() == created.json()
    grant = app.state.store.get_link(created.json()["token"])
    assert grant is not None and grant.operation_id == payload["operation_id"]

    divergent = dict(payload)
    divergent["local_intent_digest"] = "cd" * 32
    conflict = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        session_key,
        divergent,
        clock,
        cert=session_cert,
    )
    assert conflict.status_code == 409


def test_receipt_claim_rechecks_the_exact_operation_scope(
    client, app, clock, root, agent_key, agent_cert
):
    assert register(client, clock, root).status_code == 201
    publish, _, _, _ = _receipt_material(app, "publish")
    assert signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        agent_key,
        publish,
        clock,
        cert=agent_cert,
    ).status_code == 201

    revoke, _, _, _ = _receipt_material(app, "revoke")
    refused = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        agent_key,
        revoke,
        clock,
        cert=agent_cert,
    )
    assert refused.status_code == 403
    assert app.state.store.get_link_operation(ORG, revoke["operation_id"]) is None


def test_receipt_claim_bounds_public_wire_metadata_before_operation_insert(
    client, app, clock, root
):
    assert register(client, clock, root).status_code == 201
    for suffix, meta in (
        ("01", {"ttl": 366 * DAY}),
        ("02", {"label": "x" * 257}),
    ):
        payload, registry_input, proof, _ = _receipt_material(app, "publish")
        payload["operation_id"] = suffix * 32
        registry_input["operation_id"] = payload["operation_id"]
        registry_input["meta"] = meta
        payload["registry_input_digest"] = _digest(registry_input)
        refused = signed(
            client,
            "POST",
            "/v1/link-operation-receipts",
            root,
            payload,
            clock,
        )
        assert refused.status_code == 400
        assert app.state.store.get_link_operation(ORG, payload["operation_id"]) is None


def test_unknown_revoke_is_stable_and_reveals_no_foreign_token(
    client, app, clock, root, session_key, session_cert
):
    assert register(client, clock, root).status_code == 201
    payload, registry_input, proof, operand = _receipt_material(app, "revoke")
    accepted = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        session_key,
        payload,
        clock,
        cert=session_cert,
        expect=201,
    ).json()
    request = {
        "receipt": accepted["receipt"],
        "signature": accepted["signature"],
        "origin_proof": proof,
        "registry_input": registry_input,
        "operand": operand,
    }
    path = f"/v1/link-operations/{payload['operation_id']}/execute"
    first = client.post(path, json=request)
    second = client.post(path, json=request)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["state"] == "not_found"
    assert first.json()["revoked_at"] is None


def test_expired_reclaim_removes_accepted_and_completed_operation_truth(
    client, app, clock, root, session_key, session_cert
):
    assert register(client, clock, root, ttl=DAY).status_code == 201
    original_generation = app.state.store.get_org(ORG).binding_generation
    payload, registry_input, proof, _ = _receipt_material(app, "publish")
    accepted = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        session_key,
        payload,
        clock,
        cert=session_cert,
        expect=201,
    ).json()
    path = f"/v1/link-operations/{payload['operation_id']}/execute"
    result = client.post(
        path,
        json={
            "receipt": accepted["receipt"],
            "signature": accepted["signature"],
            "origin_proof": proof,
            "registry_input": registry_input,
        },
    )
    assert result.status_code == 200

    clock.advance(2 * DAY)
    reclaimed = register(client, clock, root, ttl=DAY)
    assert reclaimed.status_code == 201
    assert reclaimed.json()["binding_generation"] != original_generation
    assert app.state.store.get_link_operation(ORG, payload["operation_id"]) is None
    assert app.state.store.get_link(result.json()["token"]) is None
    assert client.post(path, json={
        "receipt": accepted["receipt"],
        "signature": accepted["signature"],
        "origin_proof": proof,
        "registry_input": registry_input,
    }).status_code == 404


def test_renewal_preserves_replay_while_rebind_blocks_first_execution(
    client, app, clock, root, session_key, session_cert
):
    assert register(client, clock, root).status_code == 201
    first, first_input, first_proof, _ = _receipt_material(app, "publish")
    first_receipt = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        session_key,
        first,
        clock,
        cert=session_cert,
        expect=201,
    ).json()
    first_path = f"/v1/link-operations/{first['operation_id']}/execute"
    first_body = {
        "receipt": first_receipt["receipt"],
        "signature": first_receipt["signature"],
        "origin_proof": first_proof,
        "registry_input": first_input,
    }
    binding = app.state.store.get_org(ORG)
    app.state.store.renew_org(
        ORG,
        now=clock.now,
        expires_at=binding.expires_at + DAY,
    )
    completed = client.post(first_path, json=first_body)
    assert completed.status_code == 200

    second, second_input, second_proof, _ = _receipt_material(app, "publish")
    second["operation_id"] = "fe" * 32
    second_input["operation_id"] = second["operation_id"]
    second["registry_input_digest"] = _digest(second_input)
    second_receipt = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        session_key,
        second,
        clock,
        cert=session_cert,
        expect=201,
    ).json()
    replacement_root = KeyPair.generate()
    app.state.store.rebind_org(
        ORG,
        root.public_hex,
        replacement_root.public_hex,
        now=clock.now,
    )
    refused = client.post(
        f"/v1/link-operations/{second['operation_id']}/execute",
        json={
            "receipt": second_receipt["receipt"],
            "signature": second_receipt["signature"],
            "origin_proof": second_proof,
            "registry_input": second_input,
        },
    )
    assert refused.status_code == 409
    assert app.state.store.get_link_operation(ORG, second["operation_id"]).state == "accepted"

    completed_replay = client.post(first_path, json=first_body)
    assert completed_replay.status_code == 200
    assert completed_replay.json() == completed.json()


def test_source_deadline_refuses_new_claim_but_not_exact_receipt_replay(clock, root):
    trusted_ms = {"now": clock.now * 1000}
    app = create_app(
        ":memory:",
        now_fn=clock,
        now_ms_fn=lambda: trusted_ms["now"],
        secure_cookies=False,
    )
    client = TestClient(app)
    assert register(client, clock, root).status_code == 201
    payload, registry_input, _, _ = _receipt_material(app, "publish")
    payload["source_expires_at_ms"] = trusted_ms["now"] + 10
    payload["target_type"] = "org:join"
    registry_input["target_uuid"] = ORG
    registry_input["target_type"] = "org:join"
    registry_input["invite_ref"] = "cd" * 32
    registry_input["source_expires_at_ms"] = payload["source_expires_at_ms"]
    registry_input["meta"].pop("ttl")
    payload["registry_input_digest"] = _digest(registry_input)

    accepted = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        root,
        payload,
        clock,
        expect=201,
    ).json()
    trusted_ms["now"] = payload["source_expires_at_ms"]
    clock.advance(1)
    replay = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        root,
        payload,
        clock,
        expect=201,
    )
    assert replay.json() == accepted

    new_payload = dict(payload)
    new_payload["operation_id"] = "ef" * 32
    expired_input = dict(registry_input)
    expired_input["operation_id"] = new_payload["operation_id"]
    new_payload["registry_input"] = expired_input
    new_payload["registry_input_digest"] = _digest(expired_input)
    expired = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        root,
        new_payload,
        clock,
    )
    assert expired.status_code == 410
    assert app.state.store.get_link_operation(ORG, new_payload["operation_id"]) is None
    client.close()


def test_source_deadline_is_only_accepted_for_org_join_publish(client, app, clock, root):
    assert register(client, clock, root).status_code == 201
    missing_deadline, _, _, _ = _receipt_material(app, "publish")
    missing_deadline["target_type"] = "org:join"
    refused_missing = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        root,
        missing_deadline,
        clock,
    )
    assert refused_missing.status_code == 400
    assert app.state.store.get_link_operation(
        ORG, missing_deadline["operation_id"]
    ) is None

    payload, _, _, _ = _receipt_material(app, "publish")
    payload["source_expires_at_ms"] = clock.now * 1000 + 60_000
    refused = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        root,
        payload,
        clock,
    )
    assert refused.status_code == 400
    assert app.state.store.get_link_operation(ORG, payload["operation_id"]) is None

    revoke, _, _, _ = _receipt_material(app, "revoke")
    revoke["source_expires_at_ms"] = clock.now * 1000 + 60_000
    refused_revoke = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        root,
        revoke,
        clock,
    )
    assert refused_revoke.status_code == 400
    assert app.state.store.get_link_operation(ORG, revoke["operation_id"]) is None


def test_org_join_execution_rejects_relative_ttl_without_creating_link(
    clock, root
):
    trusted_ms = {"now": clock.now * 1000}
    app = create_app(
        ":memory:",
        now_fn=clock,
        now_ms_fn=lambda: trusted_ms["now"],
        secure_cookies=False,
    )
    client = TestClient(app)
    assert register(client, clock, root).status_code == 201
    payload, registry_input, proof, _ = _receipt_material(app, "publish")
    payload["target_type"] = "org:join"
    payload["source_expires_at_ms"] = trusted_ms["now"] + 60_000
    registry_input.update(
        target_uuid=ORG,
        target_type="org:join",
        invite_ref="cd" * 32,
        source_expires_at_ms=payload["source_expires_at_ms"],
        meta={"ttl": 60, "label": "must use fixed expiry"},
    )
    payload["registry_input_digest"] = _digest(registry_input)
    refused_receipt = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        root,
        payload,
        clock,
    )
    assert refused_receipt.status_code == 400
    assert app.state.store.get_link_operation(
        ORG, payload["operation_id"]
    ) is None

    # Defense in depth: a historical accepted row still cannot execute an
    # org:join input that adds a relative TTL.  Claim a valid fixed-lifetime
    # input under a second operation, then tamper only the execute request.
    payload, registry_input, proof, _ = _receipt_material(app, "publish")
    payload["operation_id"] = hashlib.sha256(b"org-join-defense").hexdigest()
    registry_input["operation_id"] = payload["operation_id"]
    registry_input.update(
        target_uuid=ORG,
        target_type="org:join",
        invite_ref="cd" * 32,
        source_expires_at_ms=trusted_ms["now"] + 60_000,
        meta={"label": "fixed expiry"},
    )
    payload["target_type"] = "org:join"
    payload["source_expires_at_ms"] = registry_input["source_expires_at_ms"]
    payload["registry_input_digest"] = _digest(registry_input)
    accepted = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        root,
        payload,
        clock,
        expect=201,
    ).json()
    invalid_execute_input = dict(registry_input)
    invalid_execute_input["meta"] = {"ttl": 60, "label": "fixed expiry"}
    refused = client.post(
        f"/v1/link-operations/{payload['operation_id']}/execute",
        json={
            "receipt": accepted["receipt"],
            "signature": accepted["signature"],
            "origin_proof": proof,
            "registry_input": invalid_execute_input,
        },
    )
    assert refused.status_code == 409
    operation = app.state.store.get_link_operation(ORG, payload["operation_id"])
    assert operation.state == "accepted"
    assert operation.result_token is None
    client.close()


def test_authenticated_tunnel_consumes_same_publish_receipt_contract(
    client, app, clock, root, session_key, session_cert
):
    assert register(client, clock, root).status_code == 201
    payload, registry_input, proof, _ = _receipt_material(app, "publish")
    accepted = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        session_key,
        payload,
        clock,
        cert=session_cert,
        expect=201,
    ).json()

    class Tunnel:
        org = ORG

    args = {
        "operation_id": payload["operation_id"],
        "receipt": accepted["receipt"],
        "signature": accepted["signature"],
        "origin_proof": proof,
        "target_uuid": registry_input["target_uuid"],
        "target_type": registry_input["target_type"],
        "meta": registry_input["meta"],
    }
    with pytest.raises(relay._CtrlError, match="local-only"):
        relay._ctrl_create_link(
            Tunnel(),
            {**args, "participant_id": "must-stay-local"},
            app.state.store,
            "https://relay.auto.network",
            clock.now,
            app.state.witness_key,
        )
    assert app.state.store.get_link_operation(
        ORG, payload["operation_id"]
    ).state == "accepted"
    first = relay._ctrl_create_link(
        Tunnel(), args, app.state.store, "https://relay.auto.network", clock.now,
        app.state.witness_key,
    )
    second = relay._ctrl_create_link(
        Tunnel(), args, app.state.store, "https://relay.auto.network", clock.now,
        app.state.witness_key,
    )
    assert second == first
    assert app.state.store.get_link(first["token"]).operation_id == payload["operation_id"]


def test_authenticated_tunnel_rejects_local_only_revoke_fields_before_mutation(
    client, app, clock, root
):
    assert register(client, clock, root).status_code == 201
    token = "12" * 16
    app.state.store.create_link(
        LinkGrant(
            token=token,
            org_uuid=ORG,
            target_uuid=TARGET,
            target_type="present",
            meta={},
            created_at=clock.now,
            expires_at=None,
            revoked_at=None,
            signer_pub=root.public_hex,
            subject_kind="root",
            subject_id=root.public_hex,
        )
    )
    payload, registry_input, proof, operand = _receipt_material(app, "revoke")
    assert operand == token
    accepted = signed(
        client,
        "POST",
        "/v1/link-operation-receipts",
        root,
        payload,
        clock,
        expect=201,
    ).json()

    class Tunnel:
        org = ORG

    args = {
        "operation_id": payload["operation_id"],
        "receipt": accepted["receipt"],
        "signature": accepted["signature"],
        "origin_proof": proof,
        "token": token,
    }
    with pytest.raises(relay._CtrlError, match="local-only"):
        relay._ctrl_revoke_link(
            Tunnel(),
            {**args, "ice_policy": "must-stay-local"},
            app.state.store,
            clock.now,
            app.state.witness_key,
        )
    assert app.state.store.get_link(token).revoked_at is None
    assert app.state.store.get_link_operation(
        ORG, payload["operation_id"]
    ).state == "accepted"
    result = relay._ctrl_revoke_link(
        Tunnel(), args, app.state.store, clock.now, app.state.witness_key
    )
    assert result["state"] == "succeeded"
    assert app.state.store.get_link(token).revoked_at == clock.now
