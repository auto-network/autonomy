from __future__ import annotations

import copy
import hashlib

import pytest

from tools.network.idkit import canonical_json

from tools.graph.schemas.link_approval import (
    LINK_APPROVAL_INTENT_REVISION,
    LINK_APPROVAL_INTENT_SET_ID,
    LINK_APPROVAL_RESULT_REVISION,
    LINK_APPROVAL_RESULT_SET_ID,
)
from tools.graph.schemas.network_identity import (
    NETWORK_BINDING_REVISION,
    NETWORK_BINDING_REVISION_2,
    NETWORK_BINDING_SET_ID,
)
from tools.graph.schemas.registry import (
    SchemaValidationError,
    get_schema,
    upconvert_chain,
    validate_payload,
)


HEX32 = "12" * 16
HEX64 = "34" * 32


def _digest(value) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _intent(operation: str = "publish") -> dict:
    payload = {
        "operation": operation,
        "operation_id": HEX64,
        "target": {"target_uuid": "11111111-1111-4111-8111-111111111111", "target_type": "present"},
        "binding": {
            "org_uuid": "22222222-2222-4222-8222-222222222222",
            "root_pub": "56" * 32,
            "binding_generation": "78" * 32,
            "registry_url": "https://registry.auto.network",
        },
        "review": {"title": "Publish Mission Control"},
        "registry_input": {
            "operation_id": HEX64,
            "target_uuid": "11111111-1111-4111-8111-111111111111",
            "target_type": "present",
        },
        "registry_input_digest": "9a" * 32,
        "local_intent": {"participant_id": "member-1", "ice_policy": "relay_only"},
        "local_intent_digest": "bc" * 32,
        "origin_destination_id": "f0" * 32,
        "origin_proof_commitment": "de" * 32,
    }
    if operation == "revoke":
        payload["registry_input"] = {"operation_id": HEX64}
        payload["revoke_token"] = HEX32
        payload["operand_digest"] = _digest(
            ["autonomy.link.operand", 1, "revoke", HEX32]
        )
    payload["registry_input_digest"] = _digest(payload["registry_input"])
    payload["local_intent_digest"] = _digest(
        [
            "autonomy.link.local-intent",
            1,
            payload["target"],
            payload["binding"],
            payload["review"],
            payload["local_intent"],
            payload["origin_destination_id"],
        ]
    )
    return payload


def _binding_v2() -> dict:
    return {
        "org_uuid": "11111111-1111-4111-8111-111111111111",
        "root_pub": "ab" * 32,
        "registry_url": "https://auto.network",
        "recovery_policy": {"mode": "none"},
        "binding_expires_at": "2026-09-26T00:00:00Z",
        "binding_generation": "cd" * 32,
    }


def _org_join_intent() -> dict:
    payload = _intent()
    org_uuid = payload["binding"]["org_uuid"]
    payload["target"] = {"target_uuid": org_uuid, "target_type": "org:join"}
    payload["invite_ref"] = "ef" * 32
    payload["registry_input"] = {
        "operation_id": payload["operation_id"],
        "target_uuid": org_uuid,
        "target_type": "org:join",
        "invite_ref": payload["invite_ref"],
        "source_expires_at_ms": 1_800_000_000_000,
        "meta": {"label": "Fleet invitation"},
    }
    payload["registry_input_digest"] = _digest(payload["registry_input"])
    payload["local_intent_digest"] = _digest(
        [
            "autonomy.link.local-intent",
            1,
            payload["target"],
            payload["binding"],
            payload["review"],
            payload["local_intent"],
            payload["origin_destination_id"],
        ]
    )
    return payload


def test_binding_v2_is_registered_without_authority_inventing_upconverter():
    assert get_schema(NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION_2) is not None
    assert upconvert_chain(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, NETWORK_BINDING_REVISION_2
    ) is None
    validate_payload(NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION_2, _binding_v2())
    broken = _binding_v2()
    broken["binding_generation"] = "not-registry-authority"
    with pytest.raises(SchemaValidationError, match="binding_generation"):
        validate_payload(NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION_2, broken)


def test_link_intent_accepts_closed_publish_and_revoke_shapes():
    validate_payload(LINK_APPROVAL_INTENT_SET_ID, LINK_APPROVAL_INTENT_REVISION, _intent())
    validate_payload(
        LINK_APPROVAL_INTENT_SET_ID, LINK_APPROVAL_INTENT_REVISION, _intent("revoke")
    )


def test_revoke_intent_accepts_only_the_closed_unresolved_target_shape():
    payload = _intent("revoke")
    payload["target"] = {"resolved": False}
    payload["local_intent_digest"] = _digest(
        [
            "autonomy.link.local-intent",
            1,
            payload["target"],
            payload["binding"],
            payload["review"],
            payload["local_intent"],
            payload["origin_destination_id"],
        ]
    )
    validate_payload(
        LINK_APPROVAL_INTENT_SET_ID, LINK_APPROVAL_INTENT_REVISION, payload
    )
    payload["target"] = {"resolved": False, "target_type": "present"}
    with pytest.raises(SchemaValidationError):
        validate_payload(
            LINK_APPROVAL_INTENT_SET_ID, LINK_APPROVAL_INTENT_REVISION, payload
        )

    publish = _intent()
    publish["target"] = {"resolved": False}
    with pytest.raises(SchemaValidationError):
        validate_payload(
            LINK_APPROVAL_INTENT_SET_ID, LINK_APPROVAL_INTENT_REVISION, publish
        )
    validate_payload(
        LINK_APPROVAL_INTENT_SET_ID,
        LINK_APPROVAL_INTENT_REVISION,
        _org_join_intent(),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["registry_input"].pop("source_expires_at_ms"),
        lambda p: p["registry_input"]["meta"].update(ttl=60),
        lambda p: p["registry_input"].update(target_uuid="11111111-1111-4111-8111-111111111111"),
    ],
)
def test_org_join_intent_requires_fixed_deadline_and_binding_target(mutation):
    payload = _org_join_intent()
    mutation(payload)
    payload["registry_input_digest"] = _digest(payload["registry_input"])
    with pytest.raises(SchemaValidationError):
        validate_payload(
            LINK_APPROVAL_INTENT_SET_ID,
            LINK_APPROVAL_INTENT_REVISION,
            payload,
        )


def test_non_join_intent_rejects_even_top_level_invite_ref():
    payload = _intent()
    payload["invite_ref"] = "ef" * 32
    with pytest.raises(SchemaValidationError, match="only valid for org:join"):
        validate_payload(
            LINK_APPROVAL_INTENT_SET_ID,
            LINK_APPROVAL_INTENT_REVISION,
            payload,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(revoke_token=HEX32),
        lambda p: p["local_intent"].update(origin_proof="secret"),
        lambda p: p.update(invite_ref="not-a-public-ledger-ref"),
        lambda p: p.update(operation_id="f" * 63),
        lambda p: p.update(registry_input_digest="f" * 64),
        lambda p: p.update(local_intent_digest="e" * 64),
        lambda p: p.update(unknown="field"),
        lambda p: p["registry_input"].update(target_type="invented"),
        lambda p: p["registry_input"].update(meta={"ttl": 366 * 24 * 60 * 60}),
        lambda p: p["target"].update(target_uuid="not-a-uuid"),
    ],
)
def test_link_intent_rejects_mixed_secret_malformed_and_unknown_fields(mutation):
    payload = _intent()
    mutation(payload)
    with pytest.raises(SchemaValidationError):
        validate_payload(LINK_APPROVAL_INTENT_SET_ID, LINK_APPROVAL_INTENT_REVISION, payload)


def test_link_result_closed_success_and_failure_shapes():
    publish = {
        "operation": "publish",
        "state": "succeeded",
        "completed_at": 1_800_000_010,
        "operation_id": HEX64,
        "token": HEX32,
        "url": f"https://relay.auto.network/l/{HEX32}",
        "serving": {"live": True, "via": "tunnel-control"},
    }
    revoke = {
        "operation": "revoke",
        "state": "succeeded",
        "completed_at": 1_800_000_011,
        "operation_id": HEX64,
        "cache_removed": True,
        "registry_status": 200,
        "via": "registry-http",
        "revoked_at": 1_800_000_009,
    }
    failed = {
        "operation": "publish",
        "state": "failed",
        "completed_at": 1_800_000_012,
        "operation_id": HEX64,
        "error_code": "registry_conflict",
        "error_message": "The registry refused this operation.",
    }
    for payload in (publish, revoke, failed):
        validate_payload(LINK_APPROVAL_RESULT_SET_ID, LINK_APPROVAL_RESULT_REVISION, payload)

    for mutation in (
        lambda p: p.update(url=f"https://relay.auto.network/l/{'aa' * 16}"),
        lambda p: p.update(url=f"https://user@relay.auto.network/l/{HEX32}"),
        lambda p: p.update(error_code="not_allowed_on_success"),
        lambda p: p.update(completed_at=float("nan")),
        lambda p: p.update(completed_at=10**1000),
        lambda p: p.update(url=f"https://relay.auto.network/l/{HEX32}\ud800"),
        lambda p: p.update(revoked_at=1),
    ):
        broken = copy.deepcopy(publish)
        mutation(broken)
        with pytest.raises(SchemaValidationError):
            validate_payload(
                LINK_APPROVAL_RESULT_SET_ID, LINK_APPROVAL_RESULT_REVISION, broken
            )

    oversized_failure = copy.deepcopy(failed)
    oversized_failure["error_message"] = "x" * 513
    with pytest.raises(SchemaValidationError, match="error_message"):
        validate_payload(
            LINK_APPROVAL_RESULT_SET_ID,
            LINK_APPROVAL_RESULT_REVISION,
            oversized_failure,
        )

    oversized_serving = copy.deepcopy(publish)
    oversized_serving["serving"]["detail"] = "x" * 513
    with pytest.raises(SchemaValidationError, match="serving.detail"):
        validate_payload(
            LINK_APPROVAL_RESULT_SET_ID,
            LINK_APPROVAL_RESULT_REVISION,
            oversized_serving,
        )

    for invalid_serving in (
        {"live": True, "via": "caller-route"},
        {"live": True, "status": 99},
    ):
        broken_serving = copy.deepcopy(publish)
        broken_serving["serving"] = invalid_serving
        with pytest.raises(SchemaValidationError):
            validate_payload(
                LINK_APPROVAL_RESULT_SET_ID,
                LINK_APPROVAL_RESULT_REVISION,
                broken_serving,
            )

    reversed_revoke = copy.deepcopy(revoke)
    reversed_revoke["revoked_at"] = reversed_revoke["completed_at"] + 1
    with pytest.raises(SchemaValidationError, match="revoked_at"):
        validate_payload(
            LINK_APPROVAL_RESULT_SET_ID,
            LINK_APPROVAL_RESULT_REVISION,
            reversed_revoke,
        )
