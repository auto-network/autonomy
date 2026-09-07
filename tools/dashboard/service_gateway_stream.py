"""Admit RelayKit TLS streams to the node's local Caddy gateway.

RelayKit owns lease validation, framing, credit, fairness, and cleanup.  This
module owns the deliberately small Dashboard decision left at the seam: the
reservation must still name this host and remain active or paused, then the
stream is dialled to the fixed Compose Caddy endpoint.  HTTP remains opaque.
"""

from __future__ import annotations

import asyncio

from tools.dashboard import service_publication


LOCAL_CADDY_HOST = "service-gateway"
LOCAL_CADDY_PORT = 9443
LOCAL_CADDY_DIAL_TIMEOUT_SECONDS = 2.0


class LocalCaddyStreamHandler:
    """Resolve one leased origin and return its local Caddy TCP stream."""

    def __init__(self, org: str):
        self._org = org

    async def __call__(self, host: str, reservation: str):
        try:
            member = service_publication._reservation_for_target(
                self._org, reservation, serving=False
            )
        except service_publication.ServicePublicationError:
            return None

        payload = member.payload
        try:
            expected_host = service_publication.reservation_hostname_from_payload(payload)
        except KeyError:
            return None
        if payload.get("state") not in {"active", "paused"} or host != expected_host:
            return None

        try:
            return await asyncio.wait_for(
                asyncio.open_connection(LOCAL_CADDY_HOST, LOCAL_CADDY_PORT),
                timeout=LOCAL_CADDY_DIAL_TIMEOUT_SECONDS,
            )
        except (OSError, asyncio.TimeoutError):
            return None
