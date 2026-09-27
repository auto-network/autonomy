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
from tools.graph.schemas.serve_zone import (
    SERVE_BASE_DOMAIN,
    SERVE_ZONE_REVISION,
    SERVE_ZONE_SET_ID,
    ZONE_BINDING_KINDS,
    validate_zone_value,
)
from tools.graph.schemas.service_target import (
    DASHBOARD_ACCESS_MODE,
    DASHBOARD_TARGET_KIND,
    SESSION_TARGET_KIND,
    TARGET_KINDS,
    SERVICE_TARGET_REVISION,
    SERVICE_TARGET_SET_ID,
)


RESERVATION_UUID_NAMESPACE = "6cf440db-c8b4-566c-99db-e7be17109bdc"
_RESERVATION_NAMESPACE = uuid.UUID(RESERVATION_UUID_NAMESPACE)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class ServicePublicationError(Exception):
    """A refusal with an API code, an HTTP status, and an optional detail the
    UI may show verbatim (the registry's reason for a zone refusal)."""

    def __init__(self, code: str, status_code: int = 400, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ContainerInspection:
    container_id: str
    network_ip: str


#: The dashboard target's plain-HTTP listener (tools/dashboard/plain_listener.py).
DASHBOARD_TARGET_DEFAULT_PORT = 8081
#: What a dashboard target's route carries where a session target carries its
#: session name: the gateway route and its status line need a name, and no
#: session backs the dashboard.
DASHBOARD_TARGET_SESSION_LABEL = "dashboard"


@dataclass(frozen=True)
class ServiceTargetDescriptor:
    session_id: str
    container_id: str
    network: str
    network_ip: str
    port: int
    checked_at: str
    expires_at: str
    kind: str = SESSION_TARGET_KIND


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


def validate_zone(value: object) -> str:
    """Normalize an organization zone; ValueError on anything the registry
    would refuse (mirrors relay.validate_org_zone)."""
    try:
        return validate_zone_value(value)
    except Exception as exc:
        raise ValueError(str(exc)) from None


def zone_reservation_key(zone: str, app_label: str) -> str:
    """The reservation id for a Service directly under an organization zone.
    Byte-for-byte the registry's ``zone_reservation_id`` so the host lease the
    connector registers is the one the relay derives."""
    validate_app_label(app_label)
    zone = validate_zone(zone)
    return str(uuid.uuid5(_RESERVATION_NAMESPACE, f"zone:{zone}\0{app_label}"))


def reservation_hostname_from_payload(payload: dict) -> str:
    """The one public hostname a stored reservation denotes."""
    zone = payload.get("zone")
    if isinstance(zone, str) and zone:
        return f"{payload['app_label']}.{zone}"
    return f"{payload['app_label']}.{payload['persona_label']}.{SERVE_BASE_DOMAIN}"


def certificate_identity_for_payload(payload: dict) -> str | None:
    """The certificate identity (zone, else persona label) a reservation
    is served under; None when the row carries neither."""
    zone = payload.get("zone")
    if isinstance(zone, str) and zone:
        return zone
    persona = payload.get("persona_label")
    return persona if isinstance(persona, str) and persona else None


def _persona_for_org(org: str) -> tuple[str, str]:
    """(persona_pub, display_name) of this node's publisher in *org*.

    The PERSONAL scope publishes under the personal root itself (decision
    2026-09-27, auto-4urxx/auto-8sdrr): every machine of one identity then
    derives the same permanent label suffix with no ceremony, whereas a
    per-machine personal ledger genesis would fork the label across the
    fleet. The personal registry binding already publishes the root pub, so
    nothing is disclosed that was not. Organizations keep their ledger-derived
    personas, which is what keeps an operator's organizations unlinkable.
    """
    if org == "personal":
        from tools.graph import org_ops as _org_ops

        member = _org_ops._personal_identity_member()
        payload = getattr(member, "payload", None) or {}
        root_pub = payload.get("root_pub")
        if not isinstance(root_pub, str) or not re.fullmatch(r"[0-9a-f]{64}", root_pub):
            raise ServicePublicationError("persona_not_configured", 409)
        display_name = payload.get("display_name")
        return root_pub, display_name if isinstance(display_name, str) else ""

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
    if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value) \
            or value == DASHBOARD_TARGET_SESSION_LABEL:
        raise ServicePublicationError("invalid_session_id", 400)
    return value


def _validate_port(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ServicePublicationError("invalid_port", 400)
    return value


def _validate_kind(value: object) -> str:
    if value not in TARGET_KINDS:
        raise ServicePublicationError("invalid_kind", 400)
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
    return await _inspect_container(session_id, network, "target_session_unavailable")


def _own_dashboard_container_id() -> str | None:
    """This dashboard process's own container, or None as a host process."""
    from agents.mount_plan import _own_container_id

    return _own_container_id()


async def _inspect_dashboard_container(network: str) -> ContainerInspection:
    """The node's own dashboard container.

    The process binding the target is the process being published, so the
    container is this one: no name lookup, no compose project to guess. A
    dashboard running as a host process has no container and cannot be a
    Service target (the gateway proxies to a compose-network address).
    """
    reference = _own_dashboard_container_id()
    if not reference:
        raise ServicePublicationError("dashboard_container_unavailable", 409)
    return await _inspect_container(reference, network, "dashboard_container_unavailable")


async def _inspect_container(
    reference: str, network: str, code: str
) -> ContainerInspection:
    def inspect() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "inspect", reference],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    try:
        result = await asyncio.to_thread(inspect)
        documents = json.loads(result.stdout) if result.returncode == 0 else None
    except Exception as exc:
        raise ServicePublicationError(code, 409) from exc
    if not isinstance(documents, list) or len(documents) != 1:
        raise ServicePublicationError(code, 409)
    document = documents[0]
    if not isinstance(document, dict) or document.get("State", {}).get("Running") is not True:
        raise ServicePublicationError(code, 409)
    container_id = document.get("Id")
    if not isinstance(container_id, str) or not _HEX64_RE.fullmatch(container_id):
        raise ServicePublicationError(code, 409)
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


async def _validated_dashboard_target(
    port: object,
) -> tuple[str, str, ContainerInspection]:
    """The dashboard-kind counterpart of ``_validated_live_target``: the same
    machine pinning, compose-network and TCP-probe requirements, against this
    node's own dashboard container instead of a session's."""
    port = _validate_port(port)
    machine_id = _read_local_machine_id()
    if not machine_id:
        raise ServicePublicationError("machine_identity_unavailable", 503)
    network = discover_topology().network
    if not isinstance(network, str) or not network:
        raise ServicePublicationError("compose_network_unavailable", 503)
    inspection = await _inspect_dashboard_container(network)
    if not await _probe_tcp(inspection.network_ip, port):
        raise ServicePublicationError("target_port_unreachable", 409)
    return machine_id, network, inspection


async def _validated_target(
    org: str, kind: str, session_id: object, port: object
) -> tuple[str, str, ContainerInspection]:
    if kind == DASHBOARD_TARGET_KIND:
        return await _validated_dashboard_target(port)
    return await _validated_live_target(org, session_id, port)


def target_projection(key: str, payload: dict) -> dict:
    """One target row, as every reader sees it.

    ``machine_id`` is carried, not dropped. ServiceTargetV1 requires it and
    its docstring says the row "freezes the local machine, Dashboard session,
    container incarnation, and TCP port" -- so the binding to a machine IS the
    row's meaning, and a projection that discards it hands every reader a row
    that looks like it belongs to everyone.

    It did. The row is @home("organization") so it replicates to every member
    machine, and with machine_id stripped the gateway supervisor asked its
    connector to serve every reservation in the org. The relay grants a
    hostname to one tunnel and refuses the rest, so on registry-ash-1 that was
    716 refusals against 16 successes for one org in an hour.

    Invisible until 2026-09-10 because auto-clune.7 landed that day: before it,
    one machine per fleet ran a connector, so only one machine could act on the
    row whatever the projection said. Enabling all-machine serving turned a
    dropped field into a permanent storm.
    """
    return {
        "reservation_id": key,
        "kind": payload.get("kind", SESSION_TARGET_KIND),
        "machine_id": payload["machine_id"],
        "session_id": payload.get("session_id"),
        "port": payload["port"],
        "created_at": payload["created_at"],
        "updated_at": payload["updated_at"],
        "access_mode": payload.get("access_mode", "public"),
    }


def list_service_targets(org: str) -> list[dict]:
    rows = [
        target_projection(member.key, member.payload)
        for member in settings_ops.read_owned_set(SERVICE_TARGET_SET_ID, org=org).members
        if isinstance(member.payload, dict)
    ]
    return sorted(rows, key=lambda row: row["reservation_id"])


async def bind_service_target(
    org: str, key: str, session_id: object, port: object, access_mode: str | None = None,
    kind: object = SESSION_TARGET_KIND,
) -> tuple[dict, bool]:
    _reservation_for_target(org, key)
    kind = _validate_kind(kind)
    if kind == DASHBOARD_TARGET_KIND and session_id is not None:
        raise ServicePublicationError("unknown_fields", 400)
    machine_id, _network, inspection = await _validated_target(
        org, kind, session_id, port
    )
    session_id = _validate_session_id(session_id) if kind == SESSION_TARGET_KIND else None
    port = _validate_port(port)
    existing = _target_member_by_key(org, key)
    from tools.dashboard import service_auth
    from tools.graph.schemas.service_target import validate_access_mode

    if kind == DASHBOARD_TARGET_KIND:
        # The dashboard is gated by the personal passkey, never public or
        # org-OIDC: until that gate serves, the route renders unavailable
        # (fail closed) rather than exposing the unlock screen.
        if access_mode is None:
            access_mode = DASHBOARD_ACCESS_MODE
        elif access_mode != DASHBOARD_ACCESS_MODE:
            raise ServicePublicationError("dashboard_requires_personal", 400)
    elif access_mode is None:
        access_mode = (
            existing.payload.get("access_mode", "public") if existing is not None
            else service_auth.configuration(org)["default_access"]
        )
    try:
        validate_access_mode({"access_mode": access_mode})
    except ValueError as exc:
        raise ServicePublicationError("invalid_access_mode", 400) from exc
    if existing is not None and all(
        existing.payload.get(name) == value
        for name, value in (
            ("kind", kind),
            ("machine_id", machine_id),
            ("session_id", session_id),
            ("container_id", inspection.container_id),
            ("port", port),
            ("access_mode", access_mode),
        )
    ):
        return target_projection(key, existing.payload), False
    now = _utc_now()
    payload = {
        "kind": kind,
        "machine_id": machine_id,
        **({"session_id": session_id} if kind == SESSION_TARGET_KIND else {}),
        "container_id": inspection.container_id,
        "port": port,
        "access_mode": access_mode,
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
    kind = payload.get("kind", SESSION_TARGET_KIND)
    machine_id, network, inspection = await _validated_target(
        org, kind, payload.get("session_id"), payload.get("port")
    )
    if payload.get("machine_id") != machine_id:
        raise ServicePublicationError("target_machine_mismatch", 409)
    if payload.get("container_id") != inspection.container_id:
        # Same session_id + machine + port (all validated above); only the
        # container incarnation changed — that is exactly what a session RESUME
        # does (and, for a dashboard target, what every compose recreate of the
        # node does). Heal the frozen container id in place instead of refusing.
        # A genuinely different target carries a different session_id and
        # resolves to a different reservation key, so this never silently
        # re-points.
        payload = {**payload, "container_id": inspection.container_id,
                   "updated_at": _utc_now()}
        settings_ops.upsert_by_key(
            SERVICE_TARGET_SET_ID, SERVICE_TARGET_REVISION, key, payload, org=org)
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
        session_id=(
            payload["session_id"] if kind == SESSION_TARGET_KIND
            else DASHBOARD_TARGET_SESSION_LABEL
        ),
        container_id=inspection.container_id,
        network=network,
        network_ip=inspection.network_ip,
        port=payload["port"],
        checked_at=checked_at,
        expires_at=expires_at,
        kind=kind,
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
        "origin": f"https://{reservation_hostname_from_payload(payload)}",
        "persona_label": payload.get("persona_label"),
        "app_label": payload["app_label"],
        "state": payload["state"],
        "created_at": payload["created_at"],
        "updated_at": payload["updated_at"],
    }
    if payload.get("zone"):
        # Only organization-zone rows carry it; base-zone projections are
        # unchanged so existing consumers see exactly what they always did.
        result["zone"] = payload["zone"]
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
    earliest: tuple[str, str] | None = None
    for member in _reservation_members(org):
        payload = getattr(member, "payload", None) or {}
        if payload.get("persona_pub") != persona_pub or not payload.get("persona_label"):
            continue
        # The registry bound the label of the FIRST registration; mirror that
        # by created_at, never by iteration order (a released misbound row
        # from tonight must not win over the label bound weeks ago).
        stamp = str(payload.get("created_at") or "")
        if earliest is None or stamp < earliest[0]:
            earliest = (stamp, payload["persona_label"])
    return earliest[1] if earliest else None


def _reservation_members(org: str):
    """Raw reservation rows (payloads carry persona_pub; projections do not)."""
    return [
        member
        for member in settings_ops.read_owned_set(NAMESPACE_RESERVATION_SET_ID, org=org).members
        if isinstance(getattr(member, "payload", None), dict)
    ]


def reserve_origin(
    org: str, app_label: str, zone: str | None = None, *, persona_slug: str | None = None,
) -> tuple[dict, bool]:
    """Reserve ``<app>.<persona>.serve.auto.network`` or, with ``zone``,
    ``<app>.<zone>`` directly under a zone this organization has claimed.

    ``persona_slug`` is the operator's answer to onboarding's label question
    (already checked by remote_access.check_label): it names the persona
    label's slug for a persona that has none bound yet. A label already bound
    at the registry always wins (the registry never rebinds).
    """
    app_label = validate_app_label(app_label)
    persona_pub, display_name = _persona_for_org(org)
    if zone is not None:
        zone = validate_zone(zone)
        if active_zone(org, zone) is None:
            raise ServicePublicationError("zone_not_claimed", 409)
        key = zone_reservation_key(zone, app_label)
    else:
        key = reservation_key(persona_pub, app_label)
    existing = _member_by_key(org, key)
    if existing is not None and existing.payload.get("state") != "released":
        return reservation_projection(key, existing.payload), False
    if existing is not None:
        # RECLAIM (auto-q5xni). Released used to be terminal: the row stayed
        # under its key and every later publish of the same name answered
        # reservation_released, so a name once stopped could never be used
        # again by the organization that owns it (live 2026-09-10:
        # re-publishing oss-metrics). A released reservation now falls
        # through to a fresh upsert under the same key -- same key because
        # the relay derives the host lease id from it -- so the normal
        # publish overwrites it whole: new created_at, no released_at, and
        # the connector mints a new lease when the target is bound. The old
        # target row is dropped so the reclaimed name starts unbound instead
        # of inheriting a container that was released with it.
        stale_target = _target_member_by_key(org, key)
        if stale_target is not None:
            settings_ops.remove_setting(stale_target.id, org=org)
    now = _utc_now()
    payload = {
        "persona_pub": persona_pub,
        "app_label": app_label,
        "state": "active",
        "created_at": now,
        "updated_at": now,
    }
    if zone is not None:
        payload["zone"] = zone
    else:
        payload["persona_label"] = (
            bound_persona_label(org, persona_pub)
            or normalize_persona_label(persona_slug or display_name, persona_pub)
        )
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


# --- organization-owned delegated zones (custom domains) --------------------

_ZONE_ERROR_CODES = {
    "zone-invalid": ("zone_invalid", 400),
    "zone-unverified": ("zone_unverified", 409),
    "zone-owned-elsewhere": ("zone_owned_elsewhere", 409),
    "zone-not-owned": ("zone_not_claimed", 409),
    "not-authorized": ("zone_not_authorized", 403),
}


def _zone_members(org: str):
    return [
        member
        for member in settings_ops.read_owned_set(SERVE_ZONE_SET_ID, org=org).members
        if isinstance(getattr(member, "payload", None), dict)
    ]


def zone_projection(zone: str, payload: dict) -> dict:
    return {
        "zone": zone,
        "binding_kind": payload.get("binding_kind"),
        "state": payload.get("state"),
        "verified_at": payload.get("verified_at"),
        "claimed_at": payload.get("claimed_at"),
        "updated_at": payload.get("updated_at"),
    }


def list_zones(org: str) -> list[dict]:
    return sorted(
        (zone_projection(m.key, m.payload) for m in _zone_members(org)),
        key=lambda row: row["zone"],
    )


def active_zone(org: str, zone: str) -> dict | None:
    """The claimed, still-active zone row, or None."""
    for member in _zone_members(org):
        if member.key == zone and member.payload.get("state") == "active":
            return zone_projection(member.key, member.payload)
    return None


def _zone_control(org: str, op: str, args: dict, control=None) -> dict:
    """One control op on the org's serving tunnel; connector faults become
    a 503 the UI can explain, relay refusals map to their own codes."""
    if control is None:
        from tools.dashboard.link_serving_supervisor import control as _control
        control = _control
    try:
        reply = control(org, op, args)
    except Exception as exc:  # TunnelUnavailable, socket faults
        raise ServicePublicationError("serving_unavailable", 503, str(exc)) from exc
    if not isinstance(reply, dict):
        raise ServicePublicationError("serving_unavailable", 503, "no reply")
    if reply.get("ok") is True:
        return reply
    error = str(reply.get("error") or "refused")
    head = error.split(":", 1)[0].strip()
    code, status = _ZONE_ERROR_CODES.get(head, ("zone_refused", 409))
    detail = error.split(":", 1)[1].strip() if ":" in error else ""
    raise ServicePublicationError(code, status, detail)


def claim_zone(org: str, zone: object, binding_kind: object, *, control=None) -> tuple[dict, bool]:
    """Ask the registry to verify and claim ``zone`` for this organization,
    then record the verified claim. The registry's verdict is the authority;
    the row is only written on success."""
    zone = validate_zone(zone)
    if binding_kind not in ZONE_BINDING_KINDS:
        raise ValueError("unknown binding kind")
    reply = _zone_control(
        org, "serve.zone.claim", {"zone": zone, "binding_kind": binding_kind},
        control,
    )
    if reply.get("zone") != zone:
        raise ServicePublicationError("zone_refused", 409, "registry answered for another zone")
    now = _utc_now()
    existing = next((m for m in _zone_members(org) if m.key == zone), None)
    created = existing is None or existing.payload.get("state") != "active"
    verified_at = reply.get("verified_at")
    payload = {
        "binding_kind": binding_kind,
        "state": "active",
        "verified_at": int(verified_at) if isinstance(verified_at, int) and verified_at > 0
        else int(datetime.now(timezone.utc).timestamp()),
        "claimed_at": (existing.payload.get("claimed_at") if existing else None) or now,
        "updated_at": now,
    }
    settings_ops.upsert_by_key(
        SERVE_ZONE_SET_ID, SERVE_ZONE_REVISION, zone, payload, org=org,
    )
    return zone_projection(zone, payload), created


def release_zone(org: str, zone: object, *, control=None) -> dict:
    """Release a claimed zone. Refused while any live reservation still
    publishes under it: stop those Services first."""
    zone = validate_zone(zone)
    existing = next((m for m in _zone_members(org) if m.key == zone), None)
    if existing is None or existing.payload.get("state") != "active":
        raise ServicePublicationError("zone_not_claimed", 404)
    for member in _reservation_members(org):
        if member.payload.get("zone") == zone and member.payload.get("state") in {"active", "paused"}:
            raise ServicePublicationError("zone_in_use", 409)
    try:
        _zone_control(org, "serve.zone.release", {"zone": zone}, control)
    except ServicePublicationError as exc:
        # Already gone at the registry: the local row is still ours to close.
        if exc.code != "zone_not_claimed":
            raise
    payload = {**existing.payload, "state": "revoked", "updated_at": _utc_now()}
    settings_ops.upsert_by_key(
        SERVE_ZONE_SET_ID, SERVE_ZONE_REVISION, zone, payload, org=org,
    )
    return zone_projection(zone, payload)


def reservation_publishers(org: str) -> dict[str, dict]:
    """reservation_id -> {persona_pub, display_name} for every reservation
    row. Rows replicate organization-wide through fleet sync, so a card may
    belong to another member; the persona is the only durable owner mark."""
    from tools.graph.schemas.org_member_profile import MEMBER_PROFILE_SET_ID

    names: dict[str, str] = {}
    try:
        for member in settings_ops.read_owned_set(MEMBER_PROFILE_SET_ID, org=org).members:
            if isinstance(member.payload, dict) and isinstance(member.payload.get("display_name"), str):
                names[member.key] = member.payload["display_name"]
    except Exception:
        names = {}
    result: dict[str, dict] = {}
    for member in _reservation_members(org):
        persona_pub = member.payload.get("persona_pub")
        if not isinstance(persona_pub, str):
            continue
        result[member.key] = {
            "persona_pub": persona_pub,
            "display_name": names.get(persona_pub) or ("member " + persona_pub[:8]),
        }
    return result
