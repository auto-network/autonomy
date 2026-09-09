"""This machine's STANDING fleet sync route (auto-ekwbp).

The invitation link a joiner enrolls through expires (7 days by default),
and the first alpha reused it as the joiner's durable route -- so on
2026-09-06 an established two-machine fleet lost all sync the moment the
relay forgot the invitation. Operator ruling: an established fleet must
never lose connectivity because an invitation expired, and renewing the
invitation is not the fix.

The fix is a route the SERVING machine owns: a ``fleet:sync`` link minted
by that machine on its own tunnel, with no expiry, cached in the local
grant store so the connector's I9 gate admits it, and published to roster
peers as the ``relay_url`` of its signed reachability hint. It grants
nothing by itself: every request on it must still pass the
roster-authenticated machine handshake (``FleetAuthenticator``), enrollment
refuses any grant that is not ``fleet:join``, and artifact serving refuses
the target type. It is an address, in the same sense the invitation was
meant to be -- "a well-known place to meet" -- minus the expiry.

Idempotent: a stored self route whose grant is still cached locally and
still known to the relay is kept; otherwise one link is minted and stored.
"""

from __future__ import annotations

import logging
import time
import uuid

from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
)
from tools.network import fleet_route
from tools.network.fleet_relay_sync import (
    FleetRelaySyncError,
    _route_location,
    canonical_rendezvous,
)

logger = logging.getLogger(__name__)

STANDING_ROUTE_LABEL = "Fleet standing sync route"


class StandingRouteError(RuntimeError):
    """The standing route could not be minted or recorded."""


def _grant_cached(token: str, org) -> bool:
    from tools.dashboard.link_serving import check_grant

    return check_grant(
        token, org=org, now=time.time(), for_serving=False
    ) is not None


def _relay_knows(rendezvous: str, *, timeout: float = 10.0) -> bool | None:
    """True/False from the relay's envelope; None when the relay could not
    be asked (offline) -- an unknown answer must not trigger a re-mint."""
    import httpx

    try:
        base, _ws, token = _route_location(rendezvous)
        with httpx.Client(base_url=base, timeout=timeout) as client:
            response = client.get(f"/v1/links/{token}/envelope")
    except Exception:
        return None
    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False
    return None


def ensure_standing_route(
    *,
    machine_id: str,
    machine_pub: str,
    org="personal",
    create_link=None,
    relay_check=_relay_knows,
) -> fleet_route.FleetRoute:
    """Return this machine's standing route, minting it when needed.

    ``create_link(org, args) -> reply`` defaults to the supervisor's
    tunnel control op; tests inject a fake. ``org`` names the serving
    tunnel and the grant store: "personal" is the personal tunnel the
    fleet connector serves on (the same store the connector's I9 gate
    reads as org=None), exactly as the invitation link is published.
    """
    existing = fleet_route.load_self(org="machine")
    if existing is not None and existing.origin_machine_pub == machine_pub:
        try:
            _base, _ws, token = _route_location(existing.rendezvous)
        except FleetRelaySyncError:
            token = None
        if token is not None and _grant_cached(token, org):
            known = relay_check(existing.rendezvous)
            if known is not False:
                return existing
            logger.warning(
                "fleet standing route %s… is unknown to the relay; re-minting",
                token[:8],
            )

    if create_link is None:
        from tools.dashboard.link_approvals import _create_link_over_tunnel

        create_link = _create_link_over_tunnel
    args = {
        "target_uuid": str(uuid.uuid4()),
        "target_type": fleet_route.FLEET_ROUTE_TARGET_TYPE,
        "meta": {"label": STANDING_ROUTE_LABEL},
    }
    try:
        reply = create_link(org, args)
    except Exception as exc:
        raise StandingRouteError(f"could not mint the standing route: {exc}") from exc
    if not isinstance(reply, dict) or not reply.get("ok"):
        raise StandingRouteError(
            "relay refused the standing route: "
            f"{(reply or {}).get('error', 'no reply') if isinstance(reply, dict) else reply!r}"
        )
    url = reply.get("url")
    token = reply.get("token")
    try:
        rendezvous = canonical_rendezvous(url) if isinstance(url, str) else None
    except FleetRelaySyncError:
        rendezvous = None
    if rendezvous is None or not isinstance(token, str) or not rendezvous.endswith(token):
        raise StandingRouteError("relay returned a malformed standing route")

    grant = {
        "token": token,
        "url": rendezvous,
        "target_uuid": args["target_uuid"],
        "target_type": fleet_route.FLEET_ROUTE_TARGET_TYPE,
        "meta": {"label": STANDING_ROUTE_LABEL},
        # Attribution: the machine minted this for itself. There is no
        # persona behind it and no approval row -- the authority to serve
        # on it is the roster handshake, not this grant.
        "subject": {"kind": "machine", "id": machine_id},
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    settings_ops.upsert_by_key(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION,
        token, grant, org=org,
    )
    route = fleet_route.FleetRoute(rendezvous, machine_pub)
    fleet_route.store_self(route, org="machine")
    logger.info(
        "fleet standing route minted for machine %s: %s/l/%s…",
        machine_id[:12], rendezvous.rsplit("/l/", 1)[0], token[:8],
    )
    return route
