"""Settings-native Central runtimes for ``link_publish`` and ``link_revoke``.

The personal Central rows contain only approval authority and public review
evidence.  Raw Link operands and application results remain in the requesting
organization's Settings store.  The module is deliberately not installed in
the production registries here: activation belongs to the renderer/CLI
cutover, after these inactive runtimes and their recovery paths are proven.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import re
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from tools.dashboard import api_auth, link_approvals, network_routes
from tools.dashboard.approval_http_bridge import (
    ApprovalHttpBridgeError,
    ApprovalHttpKindAdapter,
    CanonicalLegacyDecision,
)
from tools.dashboard.approval_kind_registry import (
    ApprovalDecisionContext,
    ApprovalKindRuntime,
    ApprovalPlanningContext,
    ApprovalRequestPlan,
)
from tools.dashboard.approval_service import (
    ApprovalService,
    ApprovalServiceError,
    ApprovalStatus,
)
from tools.dashboard.attention_index_service import (
    AttentionIndexError,
    AttentionIndexService,
)
from tools.dashboard.attention_registry import (
    AttentionProjectionPlan,
    AttentionPublicationRuntime,
    AttentionSourceEvidence,
    RegisteredAttentionProducer,
)
from tools.graph import settings_ops
from tools.graph.schemas.central_attention import (
    APPROVAL_REQUEST_SET_ID,
    APPROVAL_RESOLUTION_SET_ID,
    CENTRAL_ATTENTION_REVISION,
    ApprovalRequestV1,
)
from tools.graph.schemas.link_approval import (
    LINK_APPROVAL_INTENT_REVISION,
    LINK_APPROVAL_INTENT_SET_ID,
    LINK_APPROVAL_RESULT_REVISION,
    LINK_APPROVAL_RESULT_SET_ID,
    LinkApprovalIntentV1,
    LinkApprovalResultV1,
    link_local_intent_digest,
    link_registry_input_digest,
    link_revoke_operand_digest,
)
from tools.graph.schemas.network_identity import (
    NETWORK_BINDING_REVISION,
    NETWORK_BINDING_REVISION_2,
    NETWORK_BINDING_SET_ID,
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
    NETWORK_ORG_KEY_SET_ID,
)
from tools.network.clock import MAX_CLOCK_SKEW
from tools.network.idkit import canonical_json
from tools.network.idkit.keys import verify_signature
from tools.network.registry.signing import link_operation_receipt_input


logger = logging.getLogger(__name__)

APPLICATION_SCOPE = "links"
PUBLISH_KIND = "link_publish"
REVOKE_KIND = "link_revoke"
KINDS = frozenset({PUBLISH_KIND, REVOKE_KIND})
PUBLISH_NOTIFICATION_CLASS = "approval.link_publish.requested"
REVOKE_NOTIFICATION_CLASS = "approval.link_revoke.requested"
PUBLISH_RENDERER_ID = "approval.link_publish.review"
REVOKE_RENDERER_ID = "approval.link_revoke.review"
CONSUMER_ID = "links.central-operation.v1"

_CENTRAL_ID_PREFIX = "central-"
_ATTENTION_DOMAIN = "dashboard.attention.approval-recipient"
_OPERATION_DOMAIN = "autonomy.link.operation"
_ORIGIN_PROOF_DOMAIN = "autonomy.link.origin-proof"
_ORIGIN_COMMITMENT_DOMAIN = b"autonomy.link.origin-proof-commitment.v1\n"
_DESTINATION_DOMAIN = b"dashboard.links.result-destination.v1"
_RECEIPT_PATH = "/v1/link-operation-receipts"
_EXECUTE_PATH_PREFIX = "/v1/link-operations/"
_MAX_LINK_TTL_SECONDS = 365 * 24 * 60 * 60
_MAX_PENDING_IDS = 256
_MIN_RETRY_SECONDS = 0.25
_MAX_RETRY_SECONDS = 30.0
_MAX_JSON_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 8
_MAX_JSON_NODES = 2048
_MAX_JSON_MEMBERS = 256
_MAX_JSON_KEY_BYTES = 256
_ORG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_PUBLISH_TARGET_TYPES = frozenset(
    {"present", "design", "note", "file", "mission", "org:join"}
)
_TERMINAL_RESULT_MESSAGES = {
    "operation_conflict": "The accepted Link operation conflicts with registry truth.",
    "registry_refused": "The registry permanently refused this Link operation.",
}


class LinkCentralError(RuntimeError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code.replace("_", " "))


def _json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _opaque_digest(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value)).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _storage_json_bytes(value: Any) -> bytes:
    """Canonicalize schema payloads that legitimately contain JSON floats."""

    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, UnicodeError, ValueError) as exc:
        raise LinkCentralError("settings_unavailable") from exc


def _bounded_json_mapping(value: Any, *, code: str) -> dict[str, Any]:
    """Copy one bounded JSON object without trusting arbitrary Mapping behavior."""

    nodes = 0
    active: set[int] = set()

    def copy_json(current: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise LinkCentralError(code)
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in active or len(current) > _MAX_JSON_MEMBERS:
                raise LinkCentralError(code)
            active.add(identity)
            try:
                copied: dict[str, Any] = {}
                for index, (key, child) in enumerate(current.items()):
                    if index >= _MAX_JSON_MEMBERS:
                        raise LinkCentralError(code)
                    if not isinstance(key, str):
                        raise LinkCentralError(code)
                    try:
                        encoded_key = key.encode("utf-8")
                    except UnicodeError as exc:
                        raise LinkCentralError(code) from exc
                    if not encoded_key or len(encoded_key) > _MAX_JSON_KEY_BYTES:
                        raise LinkCentralError(code)
                    copied[key] = copy_json(child, depth + 1)
                return copied
            finally:
                active.discard(identity)
        if isinstance(current, list):
            identity = id(current)
            if identity in active or len(current) > _MAX_JSON_MEMBERS:
                raise LinkCentralError(code)
            active.add(identity)
            try:
                return [copy_json(child, depth + 1) for child in current]
            finally:
                active.discard(identity)
        if current is None or isinstance(current, (str, bool, int)):
            return current
        if isinstance(current, float) and math.isfinite(current):
            return current
        raise LinkCentralError(code)

    try:
        copied = copy_json(value, 0)
        encoded = json.dumps(
            copied,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except LinkCentralError:
        raise
    except Exception as exc:
        raise LinkCentralError(code) from exc
    if not isinstance(copied, dict) or len(encoded) > _MAX_JSON_BYTES:
        raise LinkCentralError(code)
    return copied


def _resolved_secret(resolver: Callable[[], bytes]) -> bytes:
    try:
        secret = resolver()
    except Exception as exc:
        raise LinkCentralError("origin_proof_unavailable") from exc
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise LinkCentralError("origin_proof_unavailable")
    return secret


def _bounded_approval_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(_CENTRAL_ID_PREFIX)
        or value != value.strip()
    ):
        raise ValueError("invalid Central approval ID")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("invalid Central approval ID") from exc
    if not 1 <= len(encoded) <= 256 or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ValueError("invalid Central approval ID")
    return value


def link_attention_id(approval_id: str) -> str:
    approval_id = _bounded_approval_id(approval_id)
    return "attention-" + _opaque_digest([_ATTENTION_DOMAIN, 1, approval_id])


def link_operation_id(approval_id: str, operation: str) -> str:
    approval_id = _bounded_approval_id(approval_id)
    if operation not in {"publish", "revoke"}:
        raise ValueError("invalid Link operation")
    return _json_digest([_OPERATION_DOMAIN, 1, approval_id, operation])


def _session_secret() -> bytes:
    # Runtime import preserves the same manifest-rooted secret realm as the
    # dashboard-access ceremony without adding another override or key file.
    from tools.dashboard import unlock_routes

    secret = unlock_routes._session_secret()
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise LinkCentralError("origin_proof_unavailable")
    return secret


def link_origin_proof(
    approval_id: str,
    operation: str,
    *,
    secret: bytes | None = None,
) -> str:
    try:
        approval_id = _bounded_approval_id(approval_id)
    except ValueError as exc:
        raise LinkCentralError("origin_proof_unavailable") from exc
    if operation not in {"publish", "revoke"}:
        raise LinkCentralError("origin_proof_unavailable")
    key = _session_secret() if secret is None else secret
    if not isinstance(key, bytes) or len(key) < 32:
        raise LinkCentralError("origin_proof_unavailable")
    proof = hmac.new(
        key,
        canonical_json([_ORIGIN_PROOF_DOMAIN, 1, approval_id, operation]),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(proof).decode("ascii").rstrip("=")


def link_origin_proof_commitment(proof_wire: str) -> str:
    if not isinstance(proof_wire, str) or len(proof_wire) != 43 or "=" in proof_wire:
        raise LinkCentralError("origin_proof_unavailable")
    try:
        proof = base64.urlsafe_b64decode(proof_wire + "=")
    except Exception as exc:
        raise LinkCentralError("origin_proof_unavailable") from exc
    if (
        len(proof) != 32
        or base64.urlsafe_b64encode(proof).decode("ascii").rstrip("=")
        != proof_wire
    ):
        raise LinkCentralError("origin_proof_unavailable")
    return hashlib.sha256(_ORIGIN_COMMITMENT_DOMAIN + proof).hexdigest()


def link_result_destination_id(*, secret: bytes | None = None) -> str:
    key = _session_secret() if secret is None else secret
    if not isinstance(key, bytes) or len(key) < 32:
        raise LinkCentralError("origin_proof_unavailable")
    return hmac.new(key, _DESTINATION_DOMAIN, hashlib.sha256).hexdigest()


def _origin_proof_for_intent(
    approval_id: str,
    intent: Mapping[str, Any],
    resolver: Callable[[], bytes],
) -> str | None:
    """Return this origin's proof, or ``None`` for another Fleet machine.

    The destination comparison precedes every external call, durable-result
    acceptance, and result projection.  A copied personal/org row therefore
    carries no executable or disclosure authority on a machine with another
    manifest-rooted session secret.
    """

    secret = _resolved_secret(resolver)
    expected_destination = link_result_destination_id(secret=secret)
    destination = intent.get("origin_destination_id")
    if (
        not isinstance(destination, str)
        or not hmac.compare_digest(destination, expected_destination)
    ):
        return None
    proof = link_origin_proof(approval_id, intent.get("operation"), secret=secret)
    commitment = intent.get("origin_proof_commitment")
    if (
        not isinstance(commitment, str)
        or not hmac.compare_digest(
            commitment,
            link_origin_proof_commitment(proof),
        )
    ):
        raise LinkCentralError("origin_proof_mismatch")
    return proof


def _parse_iso_seconds(value: Any) -> float:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise LinkCentralError("organization_binding_unavailable")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        seconds = parsed.astimezone(timezone.utc).timestamp()
    except (OverflowError, TypeError, ValueError) as exc:
        raise LinkCentralError("organization_binding_unavailable") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise LinkCentralError("organization_binding_unavailable")
    return seconds


def _one_owned_member(set_id: str, *, org: str, key: str | None = None):
    rows = settings_ops.read_owned_set(set_id, org=org, prefix=key)
    if any(rows.dropped.values()):
        raise LinkCentralError("settings_unavailable")
    selected = [member for member in rows if key is None or member.key == key]
    if len(selected) > 1:
        raise LinkCentralError("settings_unavailable")
    return selected[0] if selected else None


def _binding_context(org: str) -> dict[str, Any] | None:
    try:
        member = network_routes._registration_binding_context(org)
    except Exception as exc:
        raise LinkCentralError("organization_binding_unavailable") from exc
    if member is None:
        return None
    payload = dict(member.payload)
    return {
        "key": member.key,
        "revision": member.stored_revision,
        "payload": payload,
    }


def _org_root_public_key(org: str) -> str:
    member = _one_owned_member(NETWORK_ORG_KEY_SET_ID, org=org)
    payload = member.payload if member is not None else None
    root = payload.get("root_pub") if isinstance(payload, Mapping) else None
    if not isinstance(root, str) or _HEX64_RE.fullmatch(root) is None:
        raise LinkCentralError("organization_key_not_configured")
    return root


def _registry_witness_public_key(registry_url: str) -> str:
    try:
        parsed = urlsplit(registry_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError
        with httpx.Client(
            base_url=registry_url.rstrip("/"), timeout=httpx.Timeout(5.0)
        ) as client:
            response = client.get("/v1/witness/pubkey")
        if response.status_code != 200 or len(response.content) > 4096:
            raise ValueError
        payload = response.json()
        witness = payload.get("witness_pub") if isinstance(payload, Mapping) else None
        if set(payload) != {"witness_pub", "v"} or payload.get("v") != 1:
            raise ValueError
        if not isinstance(witness, str) or _HEX64_RE.fullmatch(witness) is None:
            raise ValueError
        return witness
    except Exception as exc:
        raise LinkCentralError("registry_unavailable") from exc


class LinkApplicationStore:
    """Exact organization-owned intent/result access with fail-closed reads."""

    _locks = tuple(threading.RLock() for _ in range(257))

    @classmethod
    def _lock(cls, org: str, approval_id: str) -> threading.RLock:
        digest = hashlib.sha256(
            org.encode("utf-8") + b"\0" + approval_id.encode("utf-8")
        ).digest()
        return cls._locks[int.from_bytes(digest[:4], "big") % len(cls._locks)]

    def get_intent(self, org: str, approval_id: str) -> dict[str, Any] | None:
        member = _one_owned_member(
            LINK_APPROVAL_INTENT_SET_ID, org=org, key=approval_id
        )
        if member is None:
            return None
        if member.stored_revision != LINK_APPROVAL_INTENT_REVISION:
            raise LinkCentralError("settings_unavailable")
        payload = dict(member.payload)
        try:
            LinkApprovalIntentV1.validate(payload)
        except Exception as exc:
            raise LinkCentralError("settings_unavailable") from exc
        return payload

    def put_intent(self, org: str, approval_id: str, payload: Mapping[str, Any]) -> None:
        candidate = dict(payload)
        LinkApprovalIntentV1.validate(candidate)
        with self._lock(org, approval_id):
            existing = self.get_intent(org, approval_id)
            if existing is not None:
                if not hmac.compare_digest(
                    _storage_json_bytes(existing), _storage_json_bytes(candidate)
                ):
                    raise LinkCentralError("request_conflict")
                return
            settings_ops.add_setting(
                LINK_APPROVAL_INTENT_SET_ID,
                LINK_APPROVAL_INTENT_REVISION,
                approval_id,
                candidate,
                org=org,
            )

    def get_result(self, org: str, approval_id: str) -> dict[str, Any] | None:
        member = _one_owned_member(
            LINK_APPROVAL_RESULT_SET_ID, org=org, key=approval_id
        )
        if member is None:
            return None
        if member.stored_revision != LINK_APPROVAL_RESULT_REVISION:
            raise LinkCentralError("settings_unavailable")
        payload = dict(member.payload)
        try:
            LinkApprovalResultV1.validate(payload)
        except Exception as exc:
            raise LinkCentralError("settings_unavailable") from exc
        return payload

    def put_result(self, org: str, approval_id: str, payload: Mapping[str, Any]) -> None:
        candidate = dict(payload)
        LinkApprovalResultV1.validate(candidate)
        with self._lock(org, approval_id):
            existing = self.get_result(org, approval_id)
            if existing is not None:
                if not hmac.compare_digest(
                    _storage_json_bytes(existing), _storage_json_bytes(candidate)
                ):
                    raise LinkCentralError("result_conflict")
                return
            settings_ops.add_setting(
                LINK_APPROVAL_RESULT_SET_ID,
                LINK_APPROVAL_RESULT_REVISION,
                approval_id,
                candidate,
                org=org,
            )


def _bound_intent(
    request_payload: Mapping[str, Any],
    approval_id: str,
    *,
    store: LinkApplicationStore,
) -> tuple[str, dict[str, Any]]:
    """Join personal approval truth to one exact organization intent."""

    request = request_payload.get("request")
    if not isinstance(request, Mapping):
        raise LinkCentralError("settings_unavailable")
    kind = request_payload.get("kind")
    expected_operation = (
        "publish" if kind == PUBLISH_KIND else "revoke" if kind == REVOKE_KIND else None
    )
    org = request.get("org_ref")
    if (
        expected_operation is None
        or not isinstance(org, str)
        or _ORG_RE.fullmatch(org) is None
        or request.get("intent_ref") != approval_id
    ):
        raise LinkCentralError("settings_unavailable")
    expected_keys = {
        "org_ref",
        "intent_ref",
        "operation",
        "operation_id",
        "registry_input_digest",
        "local_intent_digest",
        "origin_proof_commitment",
        "origin_destination_id",
        "binding",
        "target",
    }
    if expected_operation == "revoke":
        expected_keys.add("operand_digest")
    if set(request) != expected_keys or request.get("operation") != expected_operation:
        raise LinkCentralError("settings_unavailable")
    intent = store.get_intent(org, approval_id)
    if intent is None:
        raise LinkCentralError("settings_unavailable")
    comparisons = {
        "operation": intent.get("operation"),
        "operation_id": intent.get("operation_id"),
        "registry_input_digest": intent.get("registry_input_digest"),
        "local_intent_digest": intent.get("local_intent_digest"),
        "origin_proof_commitment": intent.get("origin_proof_commitment"),
        "origin_destination_id": intent.get("origin_destination_id"),
        "binding": intent.get("binding"),
        "target": intent.get("target"),
    }
    if expected_operation == "revoke":
        comparisons["operand_digest"] = intent.get("operand_digest")
    try:
        for key, value in comparisons.items():
            if not hmac.compare_digest(
                _storage_json_bytes(request.get(key)), _storage_json_bytes(value)
            ):
                raise LinkCentralError("settings_unavailable")
    except LinkCentralError:
        raise
    except Exception as exc:
        raise LinkCentralError("settings_unavailable") from exc
    return org, intent


def _validate_org(context: ApprovalPlanningContext) -> str:
    if context.requester_principal_kind != api_auth.ApiPrincipalKind.ORG_SESSION.value:
        raise LinkCentralError("unauthenticated")
    org = context.requester_org
    if not isinstance(org, str) or _ORG_RE.fullmatch(org) is None:
        raise LinkCentralError("invalid_request")
    return org


def _validate_publish_body(body: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"target_uuid", "target_type", "meta", "invite_ref", "expires_at"}
    if not isinstance(body, Mapping) or set(body) - allowed:
        raise LinkCentralError("invalid_request")
    target_uuid = body.get("target_uuid")
    target_type = body.get("target_type")
    if not isinstance(target_uuid, str) or _UUID_RE.fullmatch(target_uuid) is None:
        raise LinkCentralError("invalid_request")
    if target_type not in _PUBLISH_TARGET_TYPES:
        raise LinkCentralError("unsupported_target")
    meta = body.get("meta", {})
    if not isinstance(meta, Mapping) or set(meta) - {
        "ttl", "label", "participant_id", "ice_policy"
    }:
        raise LinkCentralError("invalid_request")
    meta = dict(meta)
    ttl = meta.get("ttl")
    if ttl is not None and (
        type(ttl) is not int or not 1 <= ttl <= _MAX_LINK_TTL_SECONDS
    ):
        raise LinkCentralError("invalid_request")
    for field, limit in (("label", 256), ("participant_id", 256), ("ice_policy", 64)):
        value = meta.get(field)
        if value is not None:
            try:
                valid = isinstance(value, str) and len(value.encode("utf-8")) <= limit
            except UnicodeError:
                valid = False
            if not valid:
                raise LinkCentralError("invalid_request")
    if target_type == "org:join":
        invite_ref = body.get("invite_ref")
        expires_at = body.get("expires_at")
        if (
            not isinstance(invite_ref, str)
            or _HEX64_RE.fullmatch(invite_ref) is None
            or type(expires_at) is not int
            or not 0 < expires_at <= 9_007_199_254_740_991
            or "ttl" in meta
        ):
            raise LinkCentralError("invalid_request")
    elif "invite_ref" in body or "expires_at" in body:
        raise LinkCentralError("invalid_request")
    return {
        "target_uuid": target_uuid,
        "target_type": target_type,
        "meta": meta,
        **({"invite_ref": body["invite_ref"], "expires_at": body["expires_at"]}
           if target_type == "org:join" else {}),
    }


def _validate_revoke_body(body: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(body, Mapping) or set(body) != {"token"}:
        raise LinkCentralError("invalid_request")
    token = body.get("token")
    if not isinstance(token, str) or _HEX32_RE.fullmatch(token) is None:
        raise LinkCentralError("invalid_request")
    return {"token": token}


def _binding_plan(
    org: str,
    *,
    planning_time: float,
    uuid_factory: Callable[[], uuid.UUID],
    witness_resolver: Callable[[str], str],
) -> dict[str, Any]:
    found = _binding_context(org)
    if found is None:
        root_pub = _org_root_public_key(org)
        registry_url = network_routes._registry_url()
        proposed_uuid = str(uuid_factory())
        witness_pub = witness_resolver(registry_url)
        return {
            "state": "registration_required",
            "registry_key": urlsplit(registry_url).netloc,
            "registry_url": registry_url,
            "org_uuid": proposed_uuid,
            "root_pub": root_pub,
            "recovery_policy": {"mode": "none"},
            "witness_pub": witness_pub,
            "registration_payload": {
                "org_uuid": proposed_uuid,
                "root_pub": root_pub,
                "recovery_policy": {"mode": "none"},
            },
        }
    payload = found["payload"]
    registry_url = payload["registry_url"]
    witness_pub = witness_resolver(registry_url)
    expires_at = _parse_iso_seconds(payload["binding_expires_at"])
    state = "ready"
    if found["revision"] == NETWORK_BINDING_REVISION:
        state = (
            "binding_reclaim_required"
            if expires_at <= planning_time
            else "binding_generation_recovery_required"
        )
    elif found["revision"] != NETWORK_BINDING_REVISION_2:
        raise LinkCentralError("organization_binding_unavailable")
    binding = {
        "state": state,
        "registry_key": found["key"],
        "registry_url": registry_url,
        "org_uuid": payload["org_uuid"],
        "root_pub": payload["root_pub"],
        "recovery_policy": dict(payload["recovery_policy"]),
        "binding_expires_at": payload["binding_expires_at"],
        "witness_pub": witness_pub,
    }
    if found["revision"] == NETWORK_BINDING_REVISION_2:
        binding["binding_generation"] = payload["binding_generation"]
    else:
        binding["registration_payload"] = {
            "org_uuid": payload["org_uuid"],
            "root_pub": payload["root_pub"],
            "recovery_policy": dict(payload["recovery_policy"]),
        }
    return binding


def _review_for_publish(org: str, request: Mapping[str, Any], binding: Mapping[str, Any]) -> dict:
    trusted = {"org": org, **dict(request)}
    target = link_approvals._resolve_target(
        request["target_type"], request["target_uuid"], org, trusted
    )
    recipient, recipient_error = link_approvals._link_recipient(trusted)
    if target.get("error") or recipient_error:
        raise LinkCentralError("target_unavailable")
    return {
        "operation_label": "Publish link",
        "target_title": target.get("title") or request["target_uuid"],
        "target_type": request["target_type"],
        "target_type_label": link_approvals._TYPE_LABELS.get(
            request["target_type"], request["target_type"]
        ),
        "recipient": recipient,
        "organization": {"slug": org},
        "requested_ttl": request.get("meta", {}).get("ttl"),
        "fixed_expires_at_ms": request.get("expires_at"),
        "binding_state": binding["state"],
    }


def _review_for_revoke(org: str, token: str, binding: Mapping[str, Any]) -> tuple[dict, dict]:
    grant = link_approvals._cached_grant(token, org)
    target = (
        {
            "target_uuid": grant.get("target_uuid"),
            "target_type": grant.get("target_type"),
            "label": (grant.get("meta") or {}).get("label")
            if isinstance(grant.get("meta"), Mapping) else None,
            "resolved": True,
        }
        if isinstance(grant, Mapping)
        else {"resolved": False}
    )
    return ({
        "operation_label": "Revoke link",
        "target_title": target.get("label") or target.get("target_uuid") or "Unresolved link",
        "target_type": target.get("target_type"),
        "organization": {"slug": org},
        "warning": None if grant is not None else "This link is not in the local cache.",
        "binding_state": binding["state"],
    }, target)


def _build_intent(
    *,
    context: ApprovalPlanningContext,
    operation: str,
    org: str,
    target: dict,
    binding: dict,
    review: dict,
    registry_input: dict,
    local_intent: dict,
    destination_id: str,
    proof_commitment: str,
    revoke_token: str | None = None,
    invite_ref: str | None = None,
) -> dict[str, Any]:
    operation_id = link_operation_id(context.approval_id, operation)
    registry_input = dict(registry_input)
    registry_input["operation_id"] = operation_id
    payload: dict[str, Any] = {
        "operation": operation,
        "operation_id": operation_id,
        "target": dict(target),
        "binding": dict(binding),
        "review": dict(review),
        "registry_input": registry_input,
        "registry_input_digest": link_registry_input_digest(registry_input),
        "local_intent": dict(local_intent),
        "origin_destination_id": destination_id,
        "origin_proof_commitment": proof_commitment,
    }
    payload["local_intent_digest"] = link_local_intent_digest(
        target=payload["target"],
        binding=payload["binding"],
        review=payload["review"],
        local_intent=payload["local_intent"],
        origin_destination_id=destination_id,
    )
    if revoke_token is not None:
        payload["revoke_token"] = revoke_token
        payload["operand_digest"] = link_revoke_operand_digest(revoke_token)
    if invite_ref is not None:
        payload["invite_ref"] = invite_ref
    LinkApprovalIntentV1.validate(payload)
    return payload


def build_request_planner(
    kind: str,
    *,
    store: LinkApplicationStore | None = None,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    witness_resolver: Callable[[str], str] = _registry_witness_public_key,
    secret_resolver: Callable[[], bytes] = _session_secret,
):
    if kind not in KINDS:
        raise ValueError("unknown Link approval kind")
    application_store = store or LinkApplicationStore()

    def plan(context: ApprovalPlanningContext, body: Mapping[str, Any]) -> ApprovalRequestPlan:
        org = _validate_org(context)
        operation = "publish" if kind == PUBLISH_KIND else "revoke"
        request = (
            _validate_publish_body(body)
            if operation == "publish"
            else _validate_revoke_body(body)
        )
        if (
            operation == "publish"
            and request["target_type"] == "org:join"
            and request["expires_at"] / 1000 <= context.planning_time
        ):
            # The planner owns the organization intent write, so it must
            # reject stale source truth before staging anything.  The generic
            # service cannot roll that application write back later.
            raise ApprovalServiceError("source_expired")
        binding = _binding_plan(
            org,
            planning_time=context.planning_time,
            uuid_factory=uuid_factory,
            witness_resolver=witness_resolver,
        )
        secret = _resolved_secret(secret_resolver)
        proof = link_origin_proof(
            context.approval_id, operation, secret=secret
        )
        commitment = link_origin_proof_commitment(proof)
        destination_id = link_result_destination_id(secret=secret)
        operation_id = link_operation_id(context.approval_id, operation)
        if operation == "publish":
            review = _review_for_publish(org, request, binding)
            target = {
                "target_uuid": request["target_uuid"],
                "target_type": request["target_type"],
            }
            public_meta = {
                key: request["meta"][key]
                for key in ("ttl", "label")
                if key in request["meta"]
            }
            registry_input = {
                "target_uuid": request["target_uuid"],
                "target_type": request["target_type"],
                "meta": public_meta,
            }
            invite_ref = request.get("invite_ref")
            if request["target_type"] == "org:join":
                # Re-validate the source ledger using only server-derived org.
                link_approvals._org_join_request({"org": org, **request})
                registry_input.update({
                    "invite_ref": invite_ref,
                    "source_expires_at_ms": request["expires_at"],
                })
            local_intent = {
                "org": org,
                "meta": dict(request["meta"]),
                "recipient": review.get("recipient"),
            }
            intent = _build_intent(
                context=context,
                operation=operation,
                org=org,
                target=target,
                binding=binding,
                review=review,
                registry_input=registry_input,
                local_intent=local_intent,
                destination_id=destination_id,
                proof_commitment=commitment,
                invite_ref=invite_ref,
            )
            trusted_source_expires_at = (
                request["expires_at"] / 1000
                if request["target_type"] == "org:join" else None
            )
        else:
            review, target = _review_for_revoke(org, request["token"], binding)
            registry_input = {"operation_id": operation_id}
            intent = _build_intent(
                context=context,
                operation=operation,
                org=org,
                target=target,
                binding=binding,
                review=review,
                registry_input=registry_input,
                local_intent={"org": org, "route": "resolved" if target["resolved"] else "fallback"},
                destination_id=destination_id,
                proof_commitment=commitment,
                revoke_token=request["token"],
            )
            trusted_source_expires_at = None
        application_store.put_intent(org, context.approval_id, intent)
        personal_request = {
            "org_ref": org,
            "intent_ref": context.approval_id,
            "operation": operation,
            "operation_id": operation_id,
            "registry_input_digest": intent["registry_input_digest"],
            "local_intent_digest": intent["local_intent_digest"],
            "origin_proof_commitment": commitment,
            "origin_destination_id": destination_id,
            "binding": dict(binding),
            "target": dict(target),
        }
        if operation == "revoke":
            personal_request["operand_digest"] = intent["operand_digest"]
        return ApprovalRequestPlan(
            subject_ref=f"link:{operation}:{operation_id}",
            safe_review=review,
            request=personal_request,
            staged={
                "receipt_path": _RECEIPT_PATH,
                "execute_path": _EXECUTE_PATH_PREFIX + operation_id + "/execute",
            },
            trusted_source_expires_at=trusted_source_expires_at,
        )

    return plan


def _expected_receipt_payload(
    request_payload: Mapping[str, Any],
    intent: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    operation = intent["operation"]
    payload: dict[str, Any] = {
        "org_uuid": binding["org_uuid"],
        "operation": operation,
        "operation_id": intent["operation_id"],
        "binding_root_pub": binding["root_pub"],
        "binding_generation": binding["binding_generation"],
        "registry_input_digest": intent["registry_input_digest"],
        "local_intent_digest": intent["local_intent_digest"],
        "origin_proof_commitment": intent["origin_proof_commitment"],
    }
    if operation == "publish":
        payload["target_type"] = intent["target"]["target_type"]
        source_ms = intent["registry_input"].get("source_expires_at_ms")
        if source_ms is not None:
            payload["source_expires_at_ms"] = source_ms
    else:
        payload["operand_digest"] = intent["operand_digest"]
    return payload


def _current_v2_binding(intent: Mapping[str, Any], org: str) -> dict[str, Any]:
    found = _binding_context(org)
    if found is None or found["revision"] != NETWORK_BINDING_REVISION_2:
        raise LinkCentralError("organization_binding_generation_unavailable")
    current = found["payload"]
    frozen = intent["binding"]
    for key in ("registry_url", "org_uuid", "root_pub", "recovery_policy"):
        if canonical_json(current.get(key)) != canonical_json(frozen.get(key)):
            raise LinkCentralError("binding_drift")
    if frozen.get("state") == "ready" and current.get("binding_generation") != frozen.get(
        "binding_generation"
    ):
        raise LinkCentralError("binding_drift")
    if found["key"] != frozen.get("registry_key"):
        raise LinkCentralError("binding_drift")
    return dict(current)


def _require_frozen_witness(
    intent: Mapping[str, Any],
    resolver: Callable[[str], str],
) -> None:
    frozen = intent.get("binding")
    if not isinstance(frozen, Mapping):
        raise LinkCentralError("settings_unavailable")
    registry_url = frozen.get("registry_url")
    expected = frozen.get("witness_pub")
    if not isinstance(registry_url, str) or not isinstance(expected, str):
        raise LinkCentralError("settings_unavailable")
    try:
        current = resolver(registry_url)
    except LinkCentralError:
        raise
    except Exception as exc:
        raise LinkCentralError("registry_unavailable") from exc
    if (
        not isinstance(current, str)
        or _HEX64_RE.fullmatch(current) is None
        or not hmac.compare_digest(current, expected)
    ):
        raise LinkCentralError("binding_drift")


def validate_link_decision(
    context: ApprovalDecisionContext,
    request_payload: Mapping[str, Any],
    decision: Mapping[str, Any],
    is_grant: bool,
    *,
    store: LinkApplicationStore,
) -> dict[str, Any]:
    if not is_grant:
        if decision:
            raise ValueError("decline carries no decision payload")
        return {}
    try:
        decision = _bounded_json_mapping(decision, code="invalid_decision")
    except LinkCentralError as exc:
        raise ValueError("Link grant has the wrong shape") from exc
    approval_id = context.approval_id
    try:
        org, intent = _bound_intent(
            request_payload, approval_id, store=store
        )
    except LinkCentralError as exc:
        raise ValueError("Link request binding is unavailable") from exc
    binding = _current_v2_binding(intent, org)
    operation = intent["operation"]
    allowed = {"envelope", "receipt", "signature"}
    if operation == "publish":
        allowed.add("ttl")
    if set(decision) != allowed:
        raise ValueError("Link grant has the wrong shape")
    envelope = decision.get("envelope")
    receipt = decision.get("receipt")
    signature = decision.get("signature")
    if not isinstance(envelope, Mapping) or not isinstance(receipt, Mapping) or not isinstance(signature, str):
        raise ValueError("Link receipt evidence is unavailable")
    expected_payload = _expected_receipt_payload(request_payload, intent, binding)
    if envelope.get("payload") != expected_payload:
        raise ValueError("Link receipt envelope differs from frozen truth")
    if operation == "publish":
        expected_ttl = intent["registry_input"].get("meta", {}).get("ttl")
        if decision.get("ttl") != expected_ttl:
            raise ValueError("Link lifetime differs from frozen truth")
    expected_receipt = {
        "v": 1,
        "org_uuid": binding["org_uuid"],
        "binding_root_pub": binding["root_pub"],
        "binding_generation": binding["binding_generation"],
        "operation_id": intent["operation_id"],
        "operation": operation,
        "receipt_request_digest": _json_digest(expected_payload),
        "registry_input_digest": intent["registry_input_digest"],
        "local_intent_digest": intent["local_intent_digest"],
        "operand_digest": intent.get("operand_digest"),
        "origin_proof_commitment": intent["origin_proof_commitment"],
        "source_expires_at_ms": intent["registry_input"].get("source_expires_at_ms"),
    }
    for key, value in expected_receipt.items():
        if receipt.get(key) != value:
            raise ValueError("Link receipt differs from frozen truth")
    if set(receipt) != set(expected_receipt) | {
        "accepting_signer_pub", "accepting_subject_kind", "accepting_subject_id", "accepted_at"
    }:
        raise ValueError("Link receipt carries unknown fields")
    accepted_at = receipt.get("accepted_at")
    created_at = request_payload.get("created_at")
    try:
        accepted = float(accepted_at)
        created = float(created_at)
        decided = float(context.decision_time)
        valid_time = (
            not isinstance(accepted_at, bool)
            and not isinstance(created_at, bool)
            and math.isfinite(accepted)
            and math.isfinite(created)
            and math.isfinite(decided)
            and accepted >= created - MAX_CLOCK_SKEW
            and accepted <= decided + MAX_CLOCK_SKEW
        )
    except (OverflowError, TypeError, ValueError):
        valid_time = False
    if not valid_time:
        raise ValueError("Link receipt time is invalid")
    try:
        verify_signature(
            intent["binding"]["witness_pub"],
            signature,
            link_operation_receipt_input(dict(receipt)),
        )
    except Exception as exc:
        raise ValueError("Link receipt signature is invalid") from exc
    request_deadline = request_payload.get("expires_at")
    if request_deadline is not None:
        try:
            deadline = float(request_deadline)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("Link request deadline is invalid") from exc
        if (
            not math.isfinite(deadline)
            or accepted >= deadline
            or decided >= deadline
        ):
            raise ValueError("Link request has expired")
    out = {
        "envelope": dict(envelope),
        "receipt": dict(receipt),
        "signature": signature,
    }
    if operation == "publish":
        out["ttl"] = decision.get("ttl")
    return out


def build_approval_runtime(
    kind: str,
    *,
    store: LinkApplicationStore | None = None,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    witness_resolver: Callable[[str], str] = _registry_witness_public_key,
    secret_resolver: Callable[[], bytes] = _session_secret,
) -> ApprovalKindRuntime:
    application_store = store or LinkApplicationStore()
    raw_planner = build_request_planner(
        kind,
        store=application_store,
        uuid_factory=uuid_factory,
        witness_resolver=witness_resolver,
        secret_resolver=secret_resolver,
    )

    def planner(context, body):
        try:
            return raw_planner(context, body)
        except ApprovalServiceError:
            raise
        except LinkCentralError as exc:
            raise ApprovalServiceError(exc.code) from exc

    def validate(context, request, decision, is_grant):
        return validate_link_decision(
            context,
            request,
            decision,
            is_grant,
            store=application_store,
        )

    return ApprovalKindRuntime(
        request_planner=planner,
        decision_validator=validate,
        resolution_consumer_id=CONSUMER_ID,
        result_ref_builder=lambda approval_id, _request, _decision: (
            f"link-result:{approval_id}"
        ),
    )


def build_attention_runtime(kind: str, approvals: ApprovalService) -> AttentionPublicationRuntime:
    if kind not in KINDS:
        raise ValueError("unknown Link approval kind")
    operation = "publish" if kind == PUBLISH_KIND else "revoke"

    def plan(source: Any) -> AttentionProjectionPlan:
        if not isinstance(source, ApprovalStatus) or source.request.payload.get("kind") != kind:
            raise ValueError("Link projection source mismatch")
        request = source.request.payload
        resolution = source.resolution
        version = 2 if resolution is not None else 1
        review = request.get("safe_review")
        title = review.get("target_title") if isinstance(review, Mapping) else None
        return AttentionProjectionPlan(
            attention_id=link_attention_id(source.request.approval_id),
            object_ref=source.request.approval_id,
            participant_role="recipient",
            attention_state="resolved" if resolution is not None else "needs_attention",
            safe_title="Publish a share link" if operation == "publish" else "Revoke a share link",
            safe_summary=(
                f"Review {title}" if isinstance(title, str) and title else "Review Link authority"
            ),
            counterparty_ref=None,
            occurred_at=float(
                resolution.payload["resolved_at"] if resolution is not None else request["created_at"]
            ),
            source_version=version,
        )

    def evidence(object_ref: str, source_version: int) -> AttentionSourceEvidence:
        status = approvals.status(_bounded_approval_id(object_ref))
        actual = 2 if status.resolution is not None else 1
        if actual != source_version or status.request.payload.get("kind") != kind:
            raise AttentionIndexError("stale_source")
        return AttentionSourceEvidence(
            source_guard={
                "kind": "approval",
                "ref": object_ref,
                "version": source_version,
            },
            source_expires_at=status.request.payload.get("expires_at"),
        )

    return AttentionPublicationRuntime(
        projection_planner=plan,
        source_evidence_builder=evidence,
    )


@dataclass(frozen=True, slots=True)
class LinkReceiptForwarder:
    approvals: ApprovalService
    index: AttentionIndexService
    store: LinkApplicationStore
    transport: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None
    clock: Callable[[], float] = time.time
    witness_resolver: Callable[[str], str] = _registry_witness_public_key
    secret_resolver: Callable[[], bytes] = _session_secret

    def _send(self, registry_url: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.transport is not None:
            try:
                return _bounded_json_mapping(
                    self.transport(registry_url, body), code="receipt_unavailable"
                )
            except LinkCentralError:
                raise
            except Exception as exc:
                raise LinkCentralError("receipt_unavailable") from exc
        try:
            with httpx.Client(
                base_url=registry_url.rstrip("/"), timeout=httpx.Timeout(10.0)
            ) as client:
                response = client.post(_RECEIPT_PATH, json=dict(body))
            if len(response.content) > 64 * 1024:
                raise LinkCentralError("receipt_unavailable")
            if response.status_code == 410:
                raise LinkCentralError("source_expired")
            if response.status_code in {400, 401, 403, 409, 422}:
                raise LinkCentralError("invalid_decision")
            if response.status_code != 201:
                raise LinkCentralError("receipt_unavailable")
            result = response.json()
        except LinkCentralError:
            raise
        except Exception as exc:
            raise LinkCentralError("receipt_unavailable") from exc
        return _bounded_json_mapping(result, code="receipt_unavailable")

    def forward(self, attention_id: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        body = _bounded_json_mapping(body, code="invalid_request")
        if set(body) != {"envelope"}:
            raise LinkCentralError("invalid_request")
        envelope = body.get("envelope")
        if not isinstance(envelope, Mapping):
            raise LinkCentralError("invalid_request")
        try:
            item = self.index.get_query_item(attention_id)
        except AttentionIndexError as exc:
            raise LinkCentralError(
                "invalid_request" if exc.code == "invalid_request" else "unavailable"
            ) from exc
        except Exception as exc:
            raise LinkCentralError("unavailable") from exc
        if item is None:
            raise LinkCentralError("not_found")
        payload = item.payload
        if (
            payload.get("participant_role") != "recipient"
            or payload.get("attention_state") != "needs_attention"
            or payload.get("source_version") != 1
            or payload.get("application_scope") != APPLICATION_SCOPE
        ):
            raise LinkCentralError("not_actionable")
        approval_id = payload.get("object_ref")
        if not isinstance(approval_id, str):
            raise LinkCentralError("unavailable")
        try:
            status = self.approvals.status(approval_id)
        except ApprovalServiceError as exc:
            raise LinkCentralError(
                "not_found" if exc.code == "not_found" else "unavailable"
            ) from exc
        request_payload = status.request.payload
        if status.resolution is not None or request_payload.get("kind") not in KINDS:
            raise LinkCentralError("not_actionable")
        try:
            org, intent = _bound_intent(
                request_payload, approval_id, store=self.store
            )
        except LinkCentralError as exc:
            raise LinkCentralError("unavailable") from exc
        if _origin_proof_for_intent(
            approval_id,
            intent,
            self.secret_resolver,
        ) is None:
            raise LinkCentralError("not_actionable")
        binding = _current_v2_binding(intent, org)
        expected_payload = _expected_receipt_payload(
            request_payload, intent, binding
        )
        if envelope.get("payload") != expected_payload:
            raise LinkCentralError("invalid_decision")
        _require_frozen_witness(intent, self.witness_resolver)
        request_deadline = request_payload.get("expires_at")
        try:
            now = float(self.clock())
        except (OverflowError, TypeError, ValueError) as exc:
            raise LinkCentralError("unavailable") from exc
        if not math.isfinite(now) or now < 0:
            raise LinkCentralError("unavailable")
        if request_deadline is not None:
            try:
                deadline = float(request_deadline)
            except (OverflowError, TypeError, ValueError) as exc:
                raise LinkCentralError("unavailable") from exc
            if not math.isfinite(deadline):
                raise LinkCentralError("unavailable")
            if now >= deadline:
                raise LinkCentralError("source_expired")
        forwarded: dict[str, Any] = {"envelope": dict(envelope)}
        if intent["operation"] == "publish":
            forwarded["registry_input"] = dict(intent["registry_input"])
        result = self._send(intent["binding"]["registry_url"], forwarded)
        if set(result) != {"receipt", "signature"}:
            raise LinkCentralError("receipt_unavailable")
        decision: dict[str, Any] = {
            "envelope": dict(envelope),
            "receipt": result["receipt"],
            "signature": result["signature"],
        }
        if intent["operation"] == "publish":
            decision["ttl"] = intent["registry_input"].get("meta", {}).get("ttl")
        try:
            decision_time = float(self.clock())
        except (OverflowError, TypeError, ValueError) as exc:
            raise LinkCentralError("unavailable") from exc
        if not math.isfinite(decision_time) or decision_time < 0:
            raise LinkCentralError("unavailable")
        try:
            validate_link_decision(
                ApprovalDecisionContext(
                    approval_id=approval_id,
                    decision_time=decision_time,
                ),
                request_payload,
                decision,
                True,
                store=self.store,
            )
        except Exception as exc:
            raise LinkCentralError("receipt_invalid") from exc
        return dict(result)


class LinkResultConsumer:
    """Origin-bound Link execution and safe organization-result projection."""

    def __init__(
        self,
        *,
        store: LinkApplicationStore | None = None,
        secret_resolver: Callable[[], bytes] = _session_secret,
        transport: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        tunnel_transport: Callable[[str, str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
        witness_resolver: Callable[[str], str] = _registry_witness_public_key,
    ) -> None:
        self.store = store or LinkApplicationStore()
        self._secret_resolver = secret_resolver
        self._http_transport = transport
        self._tunnel_transport = tunnel_transport
        self._clock = clock
        self._witness_resolver = witness_resolver
        self._materialize_lock = threading.RLock()

    def _execute_http(
        self,
        registry_url: str,
        operation_id: str,
        body: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if self._http_transport is not None:
            try:
                result = self._http_transport(registry_url, operation_id, body)
            except Exception as exc:
                raise LinkCentralError("registry_unavailable") from exc
            if not isinstance(result, Mapping):
                raise LinkCentralError("registry_unavailable")
            return _bounded_json_mapping(result, code="registry_unavailable")
        try:
            with httpx.Client(
                base_url=registry_url.rstrip("/"), timeout=httpx.Timeout(15.0)
            ) as client:
                response = client.post(
                    f"{_EXECUTE_PATH_PREFIX}{operation_id}/execute",
                    json=dict(body),
                )
            if len(response.content) > 64 * 1024:
                raise LinkCentralError("registry_unavailable")
            if response.status_code == 409:
                return {
                    "state": "failed",
                    "error_code": "operation_conflict",
                    "error_message": "The accepted Link operation conflicts with registry truth.",
                }
            if response.status_code in {401, 403, 404, 410, 422}:
                return {
                    "state": "failed",
                    "error_code": "registry_refused",
                    "error_message": "The registry permanently refused this Link operation.",
                }
            if response.status_code != 200:
                raise LinkCentralError("registry_unavailable")
            result = response.json()
        except LinkCentralError:
            raise
        except Exception as exc:
            raise LinkCentralError("registry_unavailable") from exc
        return _bounded_json_mapping(result, code="registry_unavailable")

    def _execute_tunnel(
        self,
        org: str,
        intent: Mapping[str, Any],
        body: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        operation = intent["operation"]
        control_op = "create-link" if operation == "publish" else "revoke-link"
        args: dict[str, Any] = {
            "operation_id": intent["operation_id"],
            "receipt": body["receipt"],
            "signature": body["signature"],
            "origin_proof": body["origin_proof"],
        }
        if operation == "publish":
            registry_input = intent["registry_input"]
            args.update({
                "target_uuid": registry_input["target_uuid"],
                "target_type": registry_input["target_type"],
            })
            meta = dict(registry_input.get("meta") or {})
            if meta:
                args["meta"] = meta
        else:
            args["token"] = intent["revoke_token"]
        try:
            if self._tunnel_transport is not None:
                reply = self._tunnel_transport(org, control_op, args)
            elif operation == "publish":
                from tools.dashboard.link_serving_supervisor import get_supervisor

                started = get_supervisor().start(org)
                if not isinstance(started, Mapping) or not started.get("running"):
                    raise LinkCentralError("registry_unavailable")
                reply = link_approvals._create_link_over_tunnel(org, args)
            else:
                from tools.dashboard.link_serving_supervisor import control

                reply = control(org, control_op, args)
        except LinkCentralError:
            raise
        except Exception as exc:
            # A tunnel disconnect can occur after the registry committed.  The
            # stable operation ID makes every such outcome retryable through
            # the coordinator; it is never recorded as terminal local truth.
            raise LinkCentralError("registry_unavailable") from exc
        reply = _bounded_json_mapping(reply, code="registry_unavailable")
        if reply.get("ok") is not True:
            raise LinkCentralError("registry_unavailable")
        if operation == "publish":
            return {
                "state": "succeeded",
                "token": reply.get("token"),
                "url": reply.get("url"),
                "serving": {"live": True, "via": "tunnel-control"},
            }
        return {
            "state": reply.get("state"),
            "revoked_at": reply.get("revoked_at"),
            "via": "tunnel-control",
        }

    def _execute(
        self,
        org: str,
        intent: Mapping[str, Any],
        binding: Mapping[str, Any],
        body: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        target = intent["target"]
        use_tunnel = (
            intent["operation"] == "publish"
            and target.get("target_type") != "org:join"
        ) or (
            intent["operation"] == "revoke"
            and target.get("resolved") is True
            and target.get("target_type") != "org:join"
        )
        if use_tunnel:
            return self._execute_tunnel(org, intent, body)
        result = dict(self._execute_http(
            binding["registry_url"],
            intent["operation_id"],
            body,
        ))
        if intent["operation"] == "revoke":
            result["via"] = "registry-http"
            result.setdefault("registry_status", 200)
        return result

    @staticmethod
    def _same_result(
        row: Mapping[str, Any], *, operation: str, operation_id: str
    ) -> bool:
        return row.get("operation") == operation and row.get("operation_id") == operation_id

    def _record_terminal_failure(
        self,
        *,
        org: str,
        approval_id: str,
        operation: str,
        operation_id: str,
        code: str,
        message: str,
    ) -> bool:
        try:
            failed_at_value = self._clock()
            if isinstance(failed_at_value, bool):
                raise ValueError
            failed_at = float(failed_at_value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise LinkCentralError("registry_unavailable") from exc
        if not math.isfinite(failed_at) or failed_at < 0:
            raise LinkCentralError("registry_unavailable")
        self.store.put_result(org, approval_id, {
            "operation": operation,
            "state": "failed",
            "completed_at": failed_at,
            "operation_id": operation_id,
            "error_code": code,
            "error_message": message,
        })
        return True

    def materialize(self, status: ApprovalStatus) -> bool:
        # The production composition owns one consumer.  Serialize its local
        # replay/probe/cache/result sequence so concurrent requester reads and
        # coordinator hints cannot manufacture divergent transient serving
        # projections around the registry's idempotent operation result.
        with self._materialize_lock:
            return self._materialize(status)

    def _materialize(self, status: ApprovalStatus) -> bool:
        request_payload = status.request.payload
        resolution = status.resolution
        if request_payload.get("kind") not in KINDS or resolution is None:
            return False
        if resolution.payload.get("outcome") != "granted":
            return False
        approval_id = status.request.approval_id
        org, intent = _bound_intent(
            request_payload, approval_id, store=self.store
        )
        operation = intent["operation"]
        operation_id = intent["operation_id"]
        proof = _origin_proof_for_intent(
            approval_id,
            intent,
            self._secret_resolver,
        )
        if proof is None:
            return False
        existing = self.store.get_result(org, approval_id)
        if existing is not None:
            if not self._same_result(existing, operation=operation, operation_id=operation_id):
                raise LinkCentralError("result_conflict")
            return True
        try:
            binding = _current_v2_binding(intent, org)
        except LinkCentralError as exc:
            if exc.code != "binding_drift":
                raise
            return self._record_terminal_failure(
                org=org,
                approval_id=approval_id,
                operation=operation,
                operation_id=operation_id,
                code="binding_drift",
                message="The organization registry identity changed before execution.",
            )
        decision = resolution.payload.get("decision")
        if not isinstance(decision, Mapping):
            raise LinkCentralError("settings_unavailable")
        try:
            validated = validate_link_decision(
                ApprovalDecisionContext(
                    approval_id=approval_id,
                    decision_time=float(resolution.payload["resolved_at"]),
                ),
                request_payload,
                decision,
                True,
                store=self.store,
            )
        except Exception as exc:
            raise LinkCentralError("receipt_invalid") from exc
        try:
            _require_frozen_witness(intent, self._witness_resolver)
        except LinkCentralError as exc:
            if exc.code != "binding_drift":
                raise
            return self._record_terminal_failure(
                org=org,
                approval_id=approval_id,
                operation=operation,
                operation_id=operation_id,
                code="binding_drift",
                message="The organization registry identity changed before execution.",
            )
        execute_body: dict[str, Any] = {
            "receipt": validated["receipt"],
            "signature": validated["signature"],
            "origin_proof": proof,
            "registry_input": dict(intent["registry_input"]),
        }
        if operation == "revoke":
            execute_body["operand"] = intent["revoke_token"]
        remote = self._execute(org, intent, binding, execute_body)
        try:
            completed_value = (
                remote["completed_at"] if "completed_at" in remote else self._clock()
            )
            if isinstance(completed_value, bool):
                raise ValueError
            completed_at = float(completed_value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise LinkCentralError("registry_unavailable") from exc
        if not math.isfinite(completed_at) or completed_at < 0:
            raise LinkCentralError("registry_unavailable")
        remote_state = remote.get("state")
        if operation == "revoke" and remote_state == "not_found":
            self.store.put_result(org, approval_id, {
                "operation": operation,
                "state": "failed",
                "completed_at": completed_at,
                "operation_id": operation_id,
                "error_code": "not_found",
                "error_message": "The link was not found.",
            })
            return True
        if remote_state == "failed":
            error_code = remote.get("error_code")
            if error_code not in _TERMINAL_RESULT_MESSAGES:
                error_code = "registry_refused"
            result = {
                "operation": operation,
                "state": "failed",
                "completed_at": completed_at,
                "operation_id": operation_id,
                "error_code": error_code,
                "error_message": _TERMINAL_RESULT_MESSAGES[error_code],
            }
            self.store.put_result(org, approval_id, result)
            return True
        if remote_state != "succeeded":
            raise LinkCentralError("registry_unavailable")
        if operation == "publish":
            token = remote.get("token")
            url = remote.get("url")
            if not isinstance(token, str) or _HEX32_RE.fullmatch(token) is None:
                raise LinkCentralError("registry_unavailable")
            target = intent["target"]
            local_meta = dict(intent["local_intent"].get("meta") or {})
            receipt = validated["receipt"]
            try:
                issued_at = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(completed_at)
                )
            except (OSError, OverflowError, ValueError) as exc:
                raise LinkCentralError("registry_unavailable") from exc
            grant = {
                "token": token,
                "url": url,
                "target_uuid": target["target_uuid"],
                "target_type": target["target_type"],
                "meta": local_meta,
                "subject": {
                    "kind": receipt["accepting_subject_kind"],
                    "id": receipt["accepting_subject_id"],
                },
                "issued_at": issued_at,
            }
            if target["target_type"] == "org:join":
                grant["invite_ref"] = intent["invite_ref"]
            serving = remote.get("serving")
            if not isinstance(serving, Mapping):
                try:
                    serving = asyncio.run(
                        link_approvals._probe_serving(binding, token)
                    )
                except Exception as exc:
                    serving = {
                        "live": False,
                        "via": "registry-http",
                        "detail": f"Serving verification is pending: {exc}",
                    }
            if not isinstance(serving, Mapping) or type(serving.get("live")) is not bool:
                serving = {
                    "live": False,
                    "via": "registry-http",
                    "detail": "Serving verification is pending.",
                }
            serving = {
                key: value
                for key, value in dict(serving).items()
                if key in {"live", "via", "status"}
            }
            serving.setdefault("via", "registry-http")
            if not serving["live"]:
                serving["detail"] = "Serving verification is pending."
            result = {
                "operation": operation,
                "state": "succeeded",
                "completed_at": completed_at,
                "operation_id": operation_id,
                "token": token,
                "url": url,
                "serving": serving,
            }
            try:
                LinkApprovalResultV1.validate(result)
            except Exception as exc:
                raise LinkCentralError("registry_unavailable") from exc
            settings_ops.upsert_by_key(
                NETWORK_LINK_GRANT_SET_ID,
                NETWORK_LINK_GRANT_REVISION,
                token,
                grant,
                org=org,
            )
        else:
            revoked_at = remote.get("revoked_at")
            if isinstance(revoked_at, bool) or not isinstance(revoked_at, (int, float)):
                raise LinkCentralError("registry_unavailable")
            try:
                revoked_at = float(revoked_at)
            except (OverflowError, TypeError, ValueError) as exc:
                raise LinkCentralError("registry_unavailable") from exc
            if not math.isfinite(revoked_at) or revoked_at < 0:
                raise LinkCentralError("registry_unavailable")
            link_approvals._drop_cached_grant(intent["revoke_token"], org)
            cache_removed = link_approvals._cached_grant(
                intent["revoke_token"], org
            ) is None
            result = {
                "operation": operation,
                "state": "succeeded",
                "completed_at": completed_at,
                "operation_id": operation_id,
                "cache_removed": cache_removed,
                "via": remote.get("via"),
                "revoked_at": revoked_at,
            }
            if "registry_status" in remote:
                result["registry_status"] = remote["registry_status"]
            try:
                LinkApprovalResultV1.validate(result)
            except Exception as exc:
                raise LinkCentralError("registry_unavailable") from exc
        self.store.put_result(org, approval_id, result)
        return True

    def project(self, status: ApprovalStatus, *, operator: bool = False) -> Mapping[str, Any] | None:
        resolution = status.resolution
        if resolution is None or resolution.payload.get("outcome") != "granted":
            return None
        org, intent = _bound_intent(
            status.request.payload,
            status.request.approval_id,
            store=self.store,
        )
        if _origin_proof_for_intent(
            status.request.approval_id,
            intent,
            self._secret_resolver,
        ) is None:
            return None
        result = self.store.get_result(org, status.request.approval_id)
        if result is None:
            return None
        if not self._same_result(
            result,
            operation=intent["operation"],
            operation_id=intent["operation_id"],
        ):
            raise LinkCentralError("result_conflict")
        execution = {"ok": result["state"] == "succeeded"}
        if result["state"] == "failed":
            execution.update({
                "error": result.get("error_code"),
                "message": result.get("error_message"),
            })
            return {
                "approved": True,
                "execution": execution,
                "completed_at": result["completed_at"],
            }
        if result["operation"] == "publish":
            projected = {
                "approved": True,
                "execution": execution,
                "url": result["url"],
                "serving": dict(result["serving"]),
                "completed_at": result["completed_at"],
            }
            if not operator:
                projected["token"] = result["token"]
            return projected
        return {
            "approved": True,
            "execution": execution,
            **({} if operator else {
                "token": intent["revoke_token"]
            }),
            "cache_removed": result["cache_removed"],
            "registry_status": result.get("registry_status"),
            "via": result["via"],
            "revoked_at": result["revoked_at"],
            "completed_at": result["completed_at"],
        }


def build_http_adapter(
    kind: str,
    consumer: LinkResultConsumer,
) -> ApprovalHttpKindAdapter:
    if kind not in KINDS:
        raise ValueError("unknown Link approval kind")

    def project_request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = payload.get("request")
        if not isinstance(request, Mapping):
            raise RuntimeError("Link request is unavailable")
        approval_id = request.get("intent_ref")
        if not isinstance(approval_id, str):
            raise RuntimeError("Link request is unavailable")
        try:
            _org, intent = _bound_intent(
                payload, approval_id, store=consumer.store
            )
        except LinkCentralError as exc:
            raise RuntimeError("Link request is unavailable") from exc
        target = intent.get("target")
        if kind == PUBLISH_KIND:
            result = {
                "target_uuid": target.get("target_uuid"),
                "target_type": target.get("target_type"),
                "meta": dict(intent.get("local_intent", {}).get("meta") or {}),
            }
            if "invite_ref" in intent:
                result["invite_ref"] = intent["invite_ref"]
                result["expires_at"] = intent["registry_input"]["source_expires_at_ms"]
            return result
        return {"token": intent["revoke_token"]}

    def map_decision(body: Mapping[str, Any]) -> CanonicalLegacyDecision:
        if body == {"approved": False}:
            return CanonicalLegacyDecision("declined", {})
        if not isinstance(body, Mapping) or body.get("approved") is not True:
            raise ApprovalHttpBridgeError("invalid_decision")
        decision = dict(body)
        decision.pop("approved", None)
        return CanonicalLegacyDecision("granted", decision)

    def project_result(status: ApprovalStatus) -> Mapping[str, Any] | None:
        return consumer.project(status)

    return ApprovalHttpKindAdapter(
        kind=kind,
        request_projector=project_request,
        result_projector=project_result,
        legacy_decision_mapper=map_decision,
    )


def build_operator_result_projector(
    consumer: LinkResultConsumer,
) -> Callable[[ApprovalStatus], Mapping[str, Any] | None]:
    """Bind the no-store operator detail to the safe Link projection."""

    def project(status: ApprovalStatus) -> Mapping[str, Any] | None:
        return consumer.project(status, operator=True)

    return project


class LinkCoordinator:
    """One bounded recovery scheduler for both Link approval kinds."""

    def __init__(
        self,
        *,
        approvals: ApprovalService,
        index: AttentionIndexService,
        producers: Mapping[str, RegisteredAttentionProducer],
        consumer: LinkResultConsumer,
        private_refresh: Callable[[str], None] | None = None,
    ) -> None:
        if set(producers) != KINDS:
            raise ValueError("Link coordinator requires both exact producers")
        self.approvals = approvals
        self.index = index
        self.producers = dict(producers)
        self.consumer = consumer
        self._private_refresh = private_refresh
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._full_scan_due = False
        self._scheduled = False
        self._stopping = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._retry_seconds = _MIN_RETRY_SECONDS

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._loop is not None and self._loop is not loop:
                raise RuntimeError("Link coordinator loop changed")
            self._loop = loop
            self._stopping = False
            self._full_scan_due = True
            self._retry_seconds = _MIN_RETRY_SECONDS
            self._schedule_locked(loop)

    async def stop(self) -> None:
        with self._lock:
            self._stopping = True
            self._loop = None
            self._pending.clear()
            self._full_scan_due = False
            self._scheduled = False
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _schedule_locked(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._scheduled:
            return
        self._scheduled = True
        try:
            loop.call_soon_threadsafe(self._begin_drain)
        except RuntimeError:
            self._scheduled = False

    def offer(self, approval_id: Any) -> None:
        try:
            bounded = _bounded_approval_id(approval_id)
        except ValueError:
            return
        with self._lock:
            loop = self._loop
            if loop is None or loop.is_closed() or self._stopping:
                return
            if bounded not in self._pending and len(self._pending) >= _MAX_PENDING_IDS:
                self._pending.clear()
                self._full_scan_due = True
            else:
                self._pending.add(bounded)
            self._schedule_locked(loop)

    def offer_gap(self) -> None:
        with self._lock:
            loop = self._loop
            if loop is None or loop.is_closed() or self._stopping:
                return
            self._full_scan_due = True
            self._schedule_locked(loop)

    def offer_local_setting(self, *, operation: Any, snapshot: Any, org: Any) -> None:
        try:
            if not isinstance(snapshot, Mapping) or not isinstance(operation, str):
                return
            set_id = snapshot.get("set_id")
            revision = snapshot.get("schema_revision")
            if set_id in {APPROVAL_REQUEST_SET_ID, APPROVAL_RESOLUTION_SET_ID}:
                if (
                    org is not None
                    or isinstance(revision, bool)
                    or revision != CENTRAL_ATTENTION_REVISION
                ):
                    return
            elif set_id in {LINK_APPROVAL_INTENT_SET_ID, LINK_APPROVAL_RESULT_SET_ID}:
                if (
                    not isinstance(org, str)
                    or isinstance(revision, bool)
                    or revision != 1
                ):
                    return
            else:
                return
            self.offer(snapshot.get("key"))
        except Exception:
            self.offer_gap()

    def offer_synced(self, *, addresses: Iterable[Any] = (), gap: bool = False) -> None:
        try:
            if gap:
                self.offer_gap()
            for address in addresses:
                set_id = getattr(address, "set_id", None)
                revision = getattr(address, "schema_revision", None)
                if set_id in {
                    APPROVAL_REQUEST_SET_ID,
                    APPROVAL_RESOLUTION_SET_ID,
                    LINK_APPROVAL_INTENT_SET_ID,
                    LINK_APPROVAL_RESULT_SET_ID,
                } and not isinstance(revision, bool) and revision == 1:
                    self.offer(getattr(address, "key", None))
        except Exception:
            self.offer_gap()

    def reconcile_exact(self, approval_id: str) -> ApprovalStatus | None:
        try:
            status = self.approvals.status(_bounded_approval_id(approval_id))
        except ApprovalServiceError as exc:
            if exc.code == "not_found":
                return None
            raise
        kind = status.request.payload.get("kind")
        if kind not in KINDS:
            return None
        self.index.publish(self.producers[kind], status)
        if status.resolution is not None:
            self.consumer.materialize(status)
            after = self.consumer.project(status, operator=True)
            if after is not None and self._private_refresh is not None:
                self._private_refresh(link_attention_id(approval_id))
        return status

    def _scan_ids(self) -> tuple[tuple[str, ...], bool]:
        rows = settings_ops.read_set(APPROVAL_REQUEST_SET_ID, org=None, peers=[])
        if any(rows.dropped.values()):
            raise RuntimeError("partial Central approval request read")
        selected: list[str] = []
        incomplete = False
        for row in rows:
            try:
                if not isinstance(row.payload, dict):
                    raise ValueError
                ApprovalRequestV1.validate(row.payload)
                if row.payload.get("kind") in KINDS:
                    selected.append(_bounded_approval_id(row.key))
            except Exception:
                incomplete = True
                logger.error("Skipping one invalid Central approval request row")
        return tuple(sorted(set(selected))), incomplete

    def _begin_drain(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._stopping or self._loop is None:
                        self._scheduled = False
                        return
                    pending = tuple(sorted(self._pending))
                    self._pending.clear()
                    scan = self._full_scan_due
                    self._full_scan_due = False
                try:
                    failed = False
                    for approval_id in pending:
                        try:
                            await asyncio.to_thread(
                                self.reconcile_exact, approval_id
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            failed = True
                            logger.exception(
                                "Link Central exact reconciliation failed for %s",
                                approval_id,
                            )
                    if scan:
                        scan_ids, incomplete = await asyncio.to_thread(self._scan_ids)
                        if incomplete:
                            failed = True
                        for approval_id in scan_ids:
                            try:
                                await asyncio.to_thread(
                                    self.reconcile_exact, approval_id
                                )
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                failed = True
                                logger.exception(
                                    "Link Central scanned reconciliation failed for %s",
                                    approval_id,
                                )
                    if failed:
                        with self._lock:
                            if self._loop is not None and not self._stopping:
                                self._full_scan_due = True
                        await asyncio.sleep(self._retry_seconds)
                        self._retry_seconds = min(
                            _MAX_RETRY_SECONDS, self._retry_seconds * 2
                        )
                    else:
                        self._retry_seconds = _MIN_RETRY_SECONDS
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Link Central reconciliation failed")
                    with self._lock:
                        if self._loop is not None and not self._stopping:
                            self._full_scan_due = True
                    await asyncio.sleep(self._retry_seconds)
                    self._retry_seconds = min(
                        _MAX_RETRY_SECONDS, self._retry_seconds * 2
                    )
                with self._lock:
                    if not self._pending and not self._full_scan_due:
                        self._scheduled = False
                        return
        finally:
            with self._lock:
                if self._loop is None or self._stopping:
                    self._scheduled = False


__all__ = [
    "APPLICATION_SCOPE",
    "CONSUMER_ID",
    "KINDS",
    "LinkApplicationStore",
    "LinkCentralError",
    "LinkReceiptForwarder",
    "LinkResultConsumer",
    "LinkCoordinator",
    "PUBLISH_KIND",
    "PUBLISH_NOTIFICATION_CLASS",
    "PUBLISH_RENDERER_ID",
    "REVOKE_KIND",
    "REVOKE_NOTIFICATION_CLASS",
    "REVOKE_RENDERER_ID",
    "build_approval_runtime",
    "build_attention_runtime",
    "build_http_adapter",
    "build_operator_result_projector",
    "build_request_planner",
    "link_attention_id",
    "link_operation_id",
    "link_origin_proof",
    "link_origin_proof_commitment",
    "link_result_destination_id",
    "validate_link_decision",
]
