"""Namespace lifecycle service for sovereign web publication."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import subprocess
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agents.mount_plan import discover_topology
from tools.dashboard.dao import dashboard_db
from tools.dashboard.org_identity import session_org_slug

from tools.graph import org_ops, settings_ops
from tools.graph.schemas.machine_identity import (
    MACHINE_IDENTITY_KEY,
    MACHINE_IDENTITY_SET_ID,
)
from tools.graph.schemas.namespace_reservation import (
    NAMESPACE_RESERVATION_REVISION,
    NAMESPACE_RESERVATION_SET_ID,
    validate_app_label_value,
    validate_reservation_key,
)
from tools.graph.schemas.service_target import (
    SERVICE_TARGET_REVISION,
    SERVICE_TARGET_SET_ID,
)


RESERVATION_UUID_NAMESPACE = "6cf440db-c8b4-566c-99db-e7be17109bdc"
_RESERVATION_NAMESPACE = uuid.UUID(RESERVATION_UUID_NAMESPACE)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ServicePublicationError(Exception):
    code: str
    status_code: int


@dataclass(frozen=True)
class ContainerInspection:
    container_id: str
    network_ip: str


@dataclass(frozen=True)
class ServiceTargetDescriptor:
    session_id: str
    container_id: str
    network: str
    network_ip: str
    port: int
    checked_at: str
    expires_at: str


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


def _target_member_by_key(org: str, key: str):
    for member in settings_ops.read_owned_set(SERVICE_TARGET_SET_ID, org=org).members:
        if member.key == key and isinstance(member.payload, dict):
            return member
    return None


def _reservation_for_target(org: str, key: str, *, serving: bool = False):
    try:
        validate_reservation_key(key)
    except Exception as exc:
        raise ServicePublicationError("invalid_reservation_id", 400) from exc
    member = _member_by_key(org, key)
    if member is None:
        raise ServicePublicationError("reservation_not_found", 404)
    state = member.payload.get("state")
    if state == "released":
        raise ServicePublicationError("reservation_released", 409)
    if serving and state == "paused":
        raise ServicePublicationError("reservation_paused", 409)
    return member


def _validate_session_id(value: object) -> str:
    if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value):
        raise ServicePublicationError("invalid_session_id", 400)
    return value


def _validate_port(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ServicePublicationError("invalid_port", 400)
    return value


def _read_local_machine_id() -> str | None:
    for member in settings_ops.read_owned_set(
        MACHINE_IDENTITY_SET_ID, org="machine"
    ).members:
        if member.key == MACHINE_IDENTITY_KEY and isinstance(member.payload, dict):
            value = member.payload.get("machine_id")
            return value if isinstance(value, str) and _HEX64_RE.fullmatch(value) else None
    return None


async def _inspect_session_container(
    session_id: str, network: str
) -> ContainerInspection:
    def inspect() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "inspect", session_id],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    try:
        result = await asyncio.to_thread(inspect)
        documents = json.loads(result.stdout) if result.returncode == 0 else None
    except Exception as exc:
        raise ServicePublicationError("target_session_unavailable", 409) from exc
    if not isinstance(documents, list) or len(documents) != 1:
        raise ServicePublicationError("target_session_unavailable", 409)
    document = documents[0]
    if not isinstance(document, dict) or document.get("State", {}).get("Running") is not True:
        raise ServicePublicationError("target_session_unavailable", 409)
    container_id = document.get("Id")
    if not isinstance(container_id, str) or not _HEX64_RE.fullmatch(container_id):
        raise ServicePublicationError("target_session_unavailable", 409)
    networks = document.get("NetworkSettings", {}).get("Networks", {})
    attached = networks.get(network) if isinstance(networks, dict) else None
    ip = attached.get("IPAddress") if isinstance(attached, dict) else None
    if not isinstance(ip, str) or not ip:
        network_mode = document.get("HostConfig", {}).get("NetworkMode")
        if network_mode != "host":
            raise ServicePublicationError("target_network_unavailable", 409)
        ip = await _inspect_network_gateway(network)
    return ContainerInspection(container_id, ip)


async def _inspect_network_gateway(network: str) -> str:
    """Resolve the host as seen from one exact Docker bridge network.

    Host-network session services bind in the host namespace, so a Caddy
    container on the serving bridge reaches them through that bridge's
    gateway.  The address is Docker-owned topology, never caller input.
    """
    def inspect() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "network", "inspect", network],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    try:
        result = await asyncio.to_thread(inspect)
        documents = json.loads(result.stdout) if result.returncode == 0 else None
        configs = documents[0].get("IPAM", {}).get("Config", [])
        ipv4 = []
        for item in configs if isinstance(configs, list) else []:
            if not isinstance(item, dict):
                continue
            gateway = item.get("Gateway")
            subnet_value = item.get("Subnet")
            try:
                subnet = (
                    ipaddress.ip_network(subnet_value, strict=False)
                    if isinstance(subnet_value, str)
                    else None
                )
                if isinstance(gateway, str):
                    address = ipaddress.ip_address(gateway)
                elif subnet is not None and subnet.version == 4 \
                        and subnet.num_addresses >= 4:
                    address = subnet.network_address + 1
                else:
                    continue
            except ValueError:
                continue
            if address.version == 4 and (subnet is None or address in subnet):
                ipv4.append(address)
        address = ipv4[0] if len(ipv4) == 1 else None
    except Exception as exc:
        raise ServicePublicationError("compose_network_unavailable", 503) from exc
    if address is None or address.is_unspecified or address.is_loopback \
            or address.is_multicast:
        raise ServicePublicationError("compose_network_unavailable", 503)
    return str(address)


async def _probe_tcp(ip: str, port: int) -> bool:
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=1.0
        )
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def _validated_live_target(
    org: str, session_id: object, port: object
) -> tuple[str, str, ContainerInspection]:
    session_id = _validate_session_id(session_id)
    port = _validate_port(port)
    row = dashboard_db.get_session(session_id)
    if row is None or session_org_slug(row) != org:
        raise ServicePublicationError("target_session_not_found", 404)
    if row.get("type") == "host":
        raise ServicePublicationError("host_session_unsupported", 409)
    if not dashboard_db.is_session_live(session_id):
        raise ServicePublicationError("target_session_unavailable", 409)
    machine_id = _read_local_machine_id()
    if not machine_id:
        raise ServicePublicationError("machine_identity_unavailable", 503)
    network = discover_topology().network
    if not isinstance(network, str) or not network:
        raise ServicePublicationError("compose_network_unavailable", 503)
    inspection = await _inspect_session_container(session_id, network)
    if not await _probe_tcp(inspection.network_ip, port):
        raise ServicePublicationError("target_port_unreachable", 409)
    return machine_id, network, inspection


def target_projection(key: str, payload: dict) -> dict:
    return {
        "reservation_id": key,
        "session_id": payload["session_id"],
        "port": payload["port"],
        "created_at": payload["created_at"],
        "updated_at": payload["updated_at"],
    }


def list_service_targets(org: str) -> list[dict]:
    rows = [
        target_projection(member.key, member.payload)
        for member in settings_ops.read_owned_set(SERVICE_TARGET_SET_ID, org=org).members
        if isinstance(member.payload, dict)
    ]
    return sorted(rows, key=lambda row: row["reservation_id"])


async def bind_service_target(
    org: str, key: str, session_id: object, port: object
) -> tuple[dict, bool]:
    _reservation_for_target(org, key)
    machine_id, _network, inspection = await _validated_live_target(
        org, session_id, port
    )
    session_id = _validate_session_id(session_id)
    port = _validate_port(port)
    existing = _target_member_by_key(org, key)
    if existing is not None and all(
        existing.payload.get(name) == value
        for name, value in (
            ("machine_id", machine_id),
            ("session_id", session_id),
            ("container_id", inspection.container_id),
            ("port", port),
        )
    ):
        return target_projection(key, existing.payload), False
    now = _utc_now()
    payload = {
        "machine_id": machine_id,
        "session_id": session_id,
        "container_id": inspection.container_id,
        "port": port,
        "created_at": existing.payload["created_at"] if existing is not None else now,
        "updated_at": now,
    }
    settings_ops.upsert_by_key(
        SERVICE_TARGET_SET_ID,
        SERVICE_TARGET_REVISION,
        key,
        payload,
        org=org,
    )
    return target_projection(key, payload), existing is None


async def resolve_service_target(org: str, key: str) -> ServiceTargetDescriptor:
    _reservation_for_target(org, key, serving=True)
    member = _target_member_by_key(org, key)
    if member is None:
        raise ServicePublicationError("target_not_found", 404)
    payload = member.payload
    machine_id, network, inspection = await _validated_live_target(
        org, payload.get("session_id"), payload.get("port")
    )
    if payload.get("machine_id") != machine_id:
        raise ServicePublicationError("target_machine_mismatch", 409)
    if payload.get("container_id") != inspection.container_id:
        raise ServicePublicationError("target_stale", 409)
    checked_at = _utc_now()
    checked = datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    expires_at = (
        (checked + timedelta(seconds=5))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    return ServiceTargetDescriptor(
        session_id=payload["session_id"],
        container_id=inspection.container_id,
        network=network,
        network_ip=inspection.network_ip,
        port=payload["port"],
        checked_at=checked_at,
        expires_at=expires_at,
    )


def unbind_service_target(org: str, key: str) -> bool:
    try:
        validate_reservation_key(key)
    except Exception as exc:
        raise ServicePublicationError("invalid_reservation_id", 400) from exc
    if _member_by_key(org, key) is None:
        raise ServicePublicationError("reservation_not_found", 404)
    member = _target_member_by_key(org, key)
    if member is None:
        return False
    settings_ops.remove_setting(member.id, org=org)
    return True


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


def bound_persona_label(org: str, persona_pub: str) -> str | None:
    """The serving label this persona already publishes under, if any.

    The registry binds ONE immutable serving label per persona at its first
    host registration (relay design §3.2) and refuses every other label as
    invalid. A later display-name change must therefore not mint a new apex:
    the first reservation's label wins here exactly as it does at the relay.
    Observed 2026-09-07: a display name of "Jeremy" produced
    jeremy-<suffix> while the registry held persona-<suffix>; the gateway never
    advertised the route and five DNS-01 challenges were published under the
    bound label while ACME validated the new one, tripping the rate limit.
    """
    for row in list_reservations(org):
        if row.get("persona_pub") == persona_pub and row.get("persona_label"):
            return row["persona_label"]
    return None


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
        "persona_label": bound_persona_label(org, persona_pub)
        or normalize_persona_label(display_name, persona_pub),
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
