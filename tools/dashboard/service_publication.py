"""Namespace lifecycle service for sovereign web publication."""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from tools.graph import org_ops, settings_ops
from tools.graph.schemas.namespace_reservation import (
    NAMESPACE_RESERVATION_REVISION,
    NAMESPACE_RESERVATION_SET_ID,
    validate_app_label_value,
    validate_reservation_key,
)


RESERVATION_UUID_NAMESPACE = "6cf440db-c8b4-566c-99db-e7be17109bdc"
_RESERVATION_NAMESPACE = uuid.UUID(RESERVATION_UUID_NAMESPACE)


@dataclass(frozen=True)
class ServicePublicationError(Exception):
    code: str
    status_code: int


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def validate_app_label(value: object) -> str:
    try:
        return validate_app_label_value(value)
    except Exception as exc:
        raise ValueError("invalid app label") from exc


def normalize_persona_label(display_name: str, persona_pub: str) -> str:
    if not isinstance(persona_pub, str) or not re.fullmatch(r"[0-9a-f]{64}", persona_pub):
        raise ValueError("persona_pub must be 32-byte lowercase hex")
    source = unicodedata.normalize("NFKD", display_name if isinstance(display_name, str) else "")
    ascii_name = source.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-") or "persona"
    slug = slug[:42].rstrip("-") or "persona"
    digest = hashlib.sha256(bytes.fromhex(persona_pub)).hexdigest()[:20]
    return f"{slug}-{digest}"


def reservation_key(persona_pub: str, app_label: str) -> str:
    validate_app_label(app_label)
    if not isinstance(persona_pub, str) or not re.fullmatch(r"[0-9a-f]{64}", persona_pub):
        raise ValueError("persona_pub must be 32-byte lowercase hex")
    return str(uuid.uuid5(_RESERVATION_NAMESPACE, persona_pub + "\0" + app_label))


def _persona_for_org(org: str) -> tuple[str, str]:
    from tools.graph.schemas.org_member_profile import MEMBER_PROFILE_SET_ID
    from tools.network.ledger import LedgerStore, org_ledger_db_path

    try:
        with LedgerStore(org_ledger_db_path(org)) as store:
            if not store.ledger.genesis_id:
                raise ServicePublicationError("organization_not_founded", 409)
            state = store.fold()
            genesis_id = state.genesis_id
    except ServicePublicationError:
        raise
    except Exception as exc:
        raise ServicePublicationError("organization_ledger_unavailable", 503) from exc
    persona_pub = org_ops.persona_pub_for_org(genesis_id)
    if not persona_pub:
        raise ServicePublicationError("persona_not_configured", 409)
    display_name = ""
    for member in settings_ops.read_owned_set(MEMBER_PROFILE_SET_ID, org=org).members:
        if member.key == persona_pub and isinstance(member.payload, dict):
            value = member.payload.get("display_name")
            display_name = value if isinstance(value, str) else ""
            break
    return persona_pub, display_name


def _member_by_key(org: str, key: str):
    for member in settings_ops.read_owned_set(
        NAMESPACE_RESERVATION_SET_ID, org=org
    ).members:
        if member.key == key and isinstance(member.payload, dict):
            return member
    return None


def reservation_projection(key: str, payload: dict) -> dict:
    result = {
        "reservation_id": key,
        "origin": (
            f"https://{payload['app_label']}.{payload['persona_label']}"
            ".serve.auto.network"
        ),
        "persona_label": payload["persona_label"],
        "app_label": payload["app_label"],
        "state": payload["state"],
        "created_at": payload["created_at"],
        "updated_at": payload["updated_at"],
    }
    if payload.get("released_at"):
        result["released_at"] = payload["released_at"]
    return result


def list_reservations(org: str) -> list[dict]:
    projected = [
        reservation_projection(member.key, member.payload)
        for member in settings_ops.read_owned_set(
            NAMESPACE_RESERVATION_SET_ID, org=org
        ).members
        if isinstance(member.payload, dict)
    ]
    return sorted(projected, key=lambda row: row["origin"])


def reserve_origin(org: str, app_label: str) -> tuple[dict, bool]:
    app_label = validate_app_label(app_label)
    persona_pub, display_name = _persona_for_org(org)
    key = reservation_key(persona_pub, app_label)
    existing = _member_by_key(org, key)
    if existing is not None:
        if existing.payload.get("state") == "released":
            raise ServicePublicationError("reservation_released", 409)
        return reservation_projection(key, existing.payload), False
    now = _utc_now()
    payload = {
        "persona_pub": persona_pub,
        "persona_label": normalize_persona_label(display_name, persona_pub),
        "app_label": app_label,
        "state": "active",
        "created_at": now,
        "updated_at": now,
    }
    settings_ops.upsert_by_key(
        NAMESPACE_RESERVATION_SET_ID,
        NAMESPACE_RESERVATION_REVISION,
        key,
        payload,
        org=org,
    )
    return reservation_projection(key, payload), True


def transition_reservation(org: str, key: str, state: str) -> tuple[dict, bool]:
    try:
        validate_reservation_key(key)
    except Exception as exc:
        raise ServicePublicationError("invalid_reservation_id", 400) from exc
    if state not in {"active", "paused", "released"}:
        raise ServicePublicationError("invalid_state", 400)
    member = _member_by_key(org, key)
    if member is None:
        raise ServicePublicationError("reservation_not_found", 404)
    current = member.payload.get("state")
    if current == state:
        return reservation_projection(key, member.payload), False
    if current == "released":
        raise ServicePublicationError("reservation_released", 409)
    now = _utc_now()
    payload = {**member.payload, "state": state, "updated_at": now}
    if state == "released":
        payload["released_at"] = now
    settings_ops.upsert_by_key(
        NAMESPACE_RESERVATION_SET_ID,
        NAMESPACE_RESERVATION_REVISION,
        key,
        payload,
        org=org,
    )
    return reservation_projection(key, payload), True
