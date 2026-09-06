"""Machine-local configuration of the direct fleet-sync tier.

Reads and writes the ``autonomy.machine.fleet-direct`` row (see the schema
module for why it exists). The environment variable
``AUTONOMY_FLEET_ADVERTISE_ADDRS`` that the advertise list originally came
from is still honored and unioned in, so an existing deployment keeps
working; the Settings row is the durable, operator-visible form.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from tools.graph import settings_ops
from tools.graph.schemas.fleet_direct import (
    FLEET_DIRECT_KEY,
    FLEET_DIRECT_REVISION,
    FLEET_DIRECT_SET_ID,
    FleetDirectV1,
)

DEFAULT_LISTEN_HOST = "127.0.0.1"
ADVERTISE_ENV = "AUTONOMY_FLEET_ADVERTISE_ADDRS"


@dataclass(frozen=True)
class FleetDirectConfig:
    listen_host: str = DEFAULT_LISTEN_HOST
    listen_port: int = 0
    advertise_addrs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def enabled(self) -> bool:
        """A fixed port on a non-loopback bind is what peers can dial."""
        return self.listen_port > 0 and self.listen_host not in (
            "127.0.0.1", "localhost", "::1",
        )


def _env_advertise_addrs() -> list[str]:
    raw = os.environ.get(ADVERTISE_ENV, "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def load(*, org: str = "machine") -> FleetDirectConfig:
    """The effective direct-tier configuration (row + env union).

    Never raises: a missing or unreadable row yields the defaults, so the
    sync runtime always activates; a malformed row is treated as absent.
    """
    payload: dict = {}
    try:
        members = settings_ops.read_owned_set(
            FLEET_DIRECT_SET_ID,
            org=org,
            target_revision=FLEET_DIRECT_REVISION,
        ).to_dict()
        member = members.get(FLEET_DIRECT_KEY)
        if member is not None:
            FleetDirectV1.validate(member.payload)
            payload = dict(member.payload)
    except Exception:
        payload = {}
    addrs: list[str] = []
    for addr in list(payload.get("advertise_addrs") or []) + _env_advertise_addrs():
        if addr not in addrs:
            addrs.append(addr)
    return FleetDirectConfig(
        listen_host=payload.get("listen_host") or DEFAULT_LISTEN_HOST,
        listen_port=int(payload.get("listen_port") or 0),
        advertise_addrs=tuple(addrs),
    )


def store(config: FleetDirectConfig, *, org: str = "machine") -> None:
    payload = {
        "listen_host": config.listen_host,
        "listen_port": int(config.listen_port),
        "advertise_addrs": list(config.advertise_addrs),
    }
    FleetDirectV1.validate(payload)
    settings_ops.upsert_by_key(
        FLEET_DIRECT_SET_ID,
        FLEET_DIRECT_REVISION,
        FLEET_DIRECT_KEY,
        payload,
        org=org,
    )


def advertise_addrs(*, org: str = "machine") -> list[str]:
    """Fresh read of the advertised URLs, for announce-time getters."""
    return list(load(org=org).advertise_addrs)
