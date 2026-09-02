"""Caddy configuration boundary for sovereign Service publications.

The publication service is the authority for the session/container/port tuple.
This module accepts only its short-lived descriptor, renders a complete Caddy
configuration, and loads it over the dashboard-only Unix socket. It does not
start Caddy or watch Settings; Phase 1D's supervisor owns that lifecycle.
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import re
import socket
from dataclasses import dataclass
from typing import Mapping, Sequence

from tools.dashboard import service_publication
from tools.graph.schemas.namespace_reservation import validate_reservation_key


ADMIN_SOCKET = "/run/autonomy-service-gateway/admin.sock"
CERTIFICATE_PATH = "/run/autonomy-service-gateway-certs/tls.crt"
PRIVATE_KEY_PATH = "/run/autonomy-service-gateway-certs/tls.key"
LISTEN_PORT = 9443

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_RE = re.compile(
    rf"^(?P<app>{_LABEL})\.(?P<persona>{_LABEL})\.serve\.auto\.network$"
)
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NETWORK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class ServiceGatewayControlError(RuntimeError):
    """The local Caddy admin socket refused or could not load a config."""


@dataclass(frozen=True)
class ServiceGatewayRoute:
    reservation_id: str
    hostname: str
    session_id: str
    container_id: str
    network: str
    port: int
    expires_at: str

    def __post_init__(self) -> None:
        try:
            validate_reservation_key(self.reservation_id)
        except Exception as exc:
            raise ValueError("invalid reservation id") from exc
        if _HOST_RE.fullmatch(self.hostname) is None:
            raise ValueError("invalid Service hostname")
        if _SESSION_RE.fullmatch(self.session_id) is None:
            raise ValueError("invalid session id")
        if _HEX64_RE.fullmatch(self.container_id) is None:
            raise ValueError("invalid container id")
        if (
            _NETWORK_RE.fullmatch(self.network) is None
            or self.network in {"bridge", "host", "none"}
        ):
            raise ValueError("invalid Compose network")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("invalid target port")


async def resolve_gateway_route(org: str, reservation_id: str) -> ServiceGatewayRoute:
    """Resolve one exact active reservation through the Phase 1B authority.

    No caller supplies a hostname, upstream, container, network, or port here.
    The reservation and target Settings plus live Docker inspection supply all
    of them, and ``resolve_service_target`` returns only after the five-second
    serving check has passed.
    """
    reservation = service_publication._reservation_for_target(
        org, reservation_id, serving=True
    )
    descriptor = await service_publication.resolve_service_target(org, reservation_id)
    payload = reservation.payload
    hostname = (
        f"{payload['app_label']}.{payload['persona_label']}.serve.auto.network"
    )
    return ServiceGatewayRoute(
        reservation_id=reservation_id,
        hostname=hostname,
        session_id=descriptor.session_id,
        container_id=descriptor.container_id,
        network=descriptor.network,
        port=descriptor.port,
        expires_at=descriptor.expires_at,
    )


def reservation_hostname(org: str, reservation_id: str) -> str:
    """Return the exact stored origin hostname without accepting caller text."""
    reservation = service_publication._reservation_for_target(
        org, reservation_id, serving=False
    )
    payload = reservation.payload
    return _validate_unavailable_hostname(
        f"{payload['app_label']}.{payload['persona_label']}.serve.auto.network"
    )


def _validate_unavailable_hostname(hostname: str) -> str:
    if not isinstance(hostname, str) or _HOST_RE.fullmatch(hostname) is None:
        raise ValueError("invalid unavailable Service hostname")
    return hostname


def render_caddyfile(
    routes: Sequence[ServiceGatewayRoute],
    *,
    unavailable_hosts: Sequence[str] = (),
    certificates: Mapping[str, tuple[str, str]] | None = None,
) -> str:
    """Render the complete fail-closed gateway config.

    The only upstream tokens come from validated ``ServiceGatewayRoute``
    instances. Unknown SNI/Host combinations hit ``abort`` and never reach an
    upstream. ``unavailable_hosts`` is an exact-host tombstone used during the
    Phase 1C removal proof; Phase 1D will derive the full desired set.
    """
    routes = tuple(routes)
    unavailable_hosts = tuple(
        _validate_unavailable_hostname(host) for host in unavailable_hosts
    )
    active_names = [route.hostname for route in routes]
    if len(active_names) != len(set(active_names)):
        raise ValueError("duplicate active Service hostname")
    if len(unavailable_hosts) != len(set(unavailable_hosts)):
        raise ValueError("duplicate unavailable Service hostname")
    if set(active_names) & set(unavailable_hosts):
        raise ValueError("Service hostname cannot be active and unavailable")

    lines = [
        "{",
        f"\tadmin unix/{ADMIN_SOCKET}|0660",
        "\tpersist_config off",
        "\tauto_https off",
        f"\tservers :{LISTEN_PORT} {{",
        "\t\tprotocols h1 h2",
        "\t\tstrict_sni_host on",
        "\t}",
        "}",
        "",
    ]
    certificates = certificates or {
        hostname: (CERTIFICATE_PATH, PRIVATE_KEY_PATH)
        for hostname in (*active_names, *unavailable_hosts)
    }
    expected = set(active_names) | set(unavailable_hosts)
    if set(certificates) != expected:
        raise ValueError("certificate map must exactly cover Service hostnames")
    for hostname, pair in certificates.items():
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not all(isinstance(path, str) and path.startswith("/") for path in pair)
        ):
            raise ValueError(f"invalid certificate paths for {hostname}")

    for route in routes:
        cert_path, key_path = certificates[route.hostname]
        lines.extend(
            [
                f"https://{route.hostname}:{LISTEN_PORT} {{",
                f"\ttls {cert_path} {key_path}",
                f"\treverse_proxy {route.session_id}:{route.port} {{",
                "\t\tflush_interval -1",
                "\t}",
                "}",
                "",
            ]
        )
    for hostname in unavailable_hosts:
        cert_path, key_path = certificates[hostname]
        lines.extend(
            [
                f"https://{hostname}:{LISTEN_PORT} {{",
                f"\ttls {cert_path} {key_path}",
                '\trespond "Service unavailable" 503',
                "}",
                "",
            ]
        )
    return "\n".join(lines)


class UnixHTTPConnection(http.client.HTTPConnection):
    """Minimal stdlib HTTP client for Caddy's permissioned Unix socket."""

    def __init__(self, socket_path: str, timeout: float = 5.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self.socket_path)
        self.sock = connection


def load_caddyfile(caddyfile: str, *, timeout: float = 5.0) -> None:
    """Atomically load one complete config through Caddy's Unix admin API."""
    if not isinstance(caddyfile, str) or not caddyfile.strip():
        raise ValueError("Caddyfile must be non-empty text")
    body = caddyfile.encode("utf-8")
    connection = UnixHTTPConnection(ADMIN_SOCKET, timeout)
    try:
        connection.request(
            "POST",
            "/load",
            body=body,
            headers={
                "Content-Type": "text/caddyfile",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        response_body = response.read()
        if response.status < 200 or response.status >= 300:
            detail = response_body.decode("utf-8", errors="replace")[:1000]
            raise ServiceGatewayControlError(
                f"Caddy config load failed ({response.status} {response.reason}): "
                f"{detail}"
            )
    except ServiceGatewayControlError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise ServiceGatewayControlError(
            f"Caddy admin socket unavailable: {exc}"
        ) from exc
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load one validated Service route into local Caddy"
    )
    parser.add_argument("mode", choices=("load", "unavailable"))
    parser.add_argument("--org", required=True)
    parser.add_argument("--reservation-id", required=True)
    args = parser.parse_args()

    if args.mode == "load":
        route = asyncio.run(resolve_gateway_route(args.org, args.reservation_id))
        config = render_caddyfile([route])
        hostname = route.hostname
        result = {
            "ok": True,
            "mode": "active",
            "hostname": hostname,
            "session_id": route.session_id,
            "port": route.port,
        }
    else:
        hostname = reservation_hostname(args.org, args.reservation_id)
        config = render_caddyfile([], unavailable_hosts=[hostname])
        result = {"ok": True, "mode": "unavailable", "hostname": hostname}
    load_caddyfile(config)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
