"""This machine's direct fleet-sync listener: where it binds and what it
advertises to roster peers.

The direct tier is the cheapest path between two fleet machines: a
roster-authenticated WebSocket straight to the peer, no relay in the
middle. It shipped bound to ``127.0.0.1:0`` with its advertised addresses
read only from an environment variable, so it never engaged on a real
fleet. This row is the operator-facing switch: a fixed bind and the
externally reachable URLs peers should dial. It is machine-local because
each machine has its own network position; it never synchronizes.

Announcing an address grants nothing: a dial still has to pass the
roster-authenticated machine handshake, and the registry serves these
hints only to roster machines holding a ``node:lookup`` cert.
"""

from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)

FLEET_DIRECT_SET_ID = "autonomy.machine.fleet-direct"
FLEET_DIRECT_REVISION = 1
FLEET_DIRECT_KEY = "self"

MAX_ADVERTISE_ADDRS = 8
MAX_ADDR_LEN = 512

SYNOPSIS = {
    "summary": (
        "This machine's direct fleet-sync listener bind (host, port) and the "
        "ws/wss URLs it advertises to roster peers through registry "
        "reachability hints. Machine-local; never synced. Port 0 keeps the "
        "listener ephemeral (loopback only), which disables the direct tier."
    ),
    "nouns": [
        "fleet direct", "direct tier", "fleet listener", "advertise address",
        "reachability announce",
    ],
    "related_set_ids": [
        "autonomy.machine.identity#1", "autonomy.machine.fleet-route#1",
    ],
}


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="machine_self")
class FleetDirectV1(SettingSchema):
    """The one row describing this machine's direct listener."""

    set_id = FLEET_DIRECT_SET_ID
    schema_revision = FLEET_DIRECT_REVISION

    listen_host: str = field(
        required=False,
        description=(
            "Interface the direct listener binds. Default 127.0.0.1; use "
            "0.0.0.0 or a specific interface address to accept peers."
        ),
    )
    listen_port: int = field(
        required=False,
        description=(
            "Fixed TCP port for the direct listener (1-65535). 0 or absent "
            "means ephemeral, which peers cannot dial."
        ),
    )
    advertise_addrs: list = field(
        required=False,
        description=(
            "ws:// or wss:// URLs roster peers should dial to reach this "
            "listener, announced through registry reachability hints. At "
            "most 8. Empty means this machine is not dialable directly."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        host = payload.get("listen_host")
        if host is not None and (
            not isinstance(host, str) or not host or len(host) > 253
            or any(c.isspace() for c in host)
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'listen_host' must be a non-empty host string"
            )
        port = payload.get("listen_port")
        if port is not None and (
            type(port) is not int or port < 0 or port > 65535
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'listen_port' must be an integer 0-65535"
            )
        addrs = payload.get("advertise_addrs")
        if addrs is not None:
            if not isinstance(addrs, list) or len(addrs) > MAX_ADVERTISE_ADDRS:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'advertise_addrs' must be a list of at "
                    f"most {MAX_ADVERTISE_ADDRS} URLs"
                )
            for addr in addrs:
                if (
                    not isinstance(addr, str)
                    or not addr.startswith(("ws://", "wss://"))
                    or len(addr) > MAX_ADDR_LEN
                    or any(c.isspace() for c in addr)
                ):
                    raise SchemaValidationError(
                        f"{cls.__name__}: every 'advertise_addrs' entry must be "
                        "a ws:// or wss:// URL"
                    )
