"""Fleet crosstalk (Path A prototype): a personal-scope directive that rides
Settings replication, delivered event-based on the far node.

A :class:`FleetCrosstalkV1` row is a personal-homed Setting, and the personal
store "follows the operator across every machine they own" (``home('personal')``),
so the row replicates to the operator's other machines by the EXISTING fleet
sync — no new transport, no session→machine registry. On the machine that WROTE
it the :mod:`crosstalk_directive` mediator already delivers it. But a
SYNC-APPLIED write does not fire ``setting.changed`` on the receiver
(``tools/network/fleet_sync/materialize.py`` writes rows with a direct INSERT,
bypassing the emit hook — see note ``296b289a``), so the far node would never
deliver on its own.

This module closes exactly that gap, and ONLY for this one set. It consumes the
event-based ``_emit_settings_materialized`` signal the sync scheduler already
fires on every apply (no timer, no poll), and for a materialized FleetCrosstalk
address whose target session is LOCAL and live, pastes the body in via the same
tmux delivery the mediator uses. Every node runs this on the same synced row;
the one that owns the target session delivers and the rest no-op — so the owner
self-selects, which is how routing happens with no registry.

Scoped to :data:`FLEET_CROSSTALK_SET_ID`; no other set's behaviour changes.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Iterable

from tools.dashboard.crosstalk_directive import (
    CROSSTALK_DIRECTIVE_NAMESPACE,
    CrosstalkDirective,
)
from tools.graph.schemas.registry import home

logger = logging.getLogger(__name__)

FLEET_CROSSTALK_SUFFIX = "fleet-message"
FLEET_CROSSTALK_SET_ID = f"{CROSSTALK_DIRECTIVE_NAMESPACE}.{FLEET_CROSSTALK_SUFFIX}"
_PERSONAL = "personal"


@home("personal")
class FleetCrosstalkV1(CrosstalkDirective):
    """A crosstalk message that crosses the operator's own fleet.

    Personal-homed so the row replicates to every machine the operator owns.
    Inherits the base ``deliver`` — ``session_send(target_session, body)`` — so
    on the machine that wrote it, the mediator pastes ``body`` into
    ``target_session`` immediately; on a machine that RECEIVES it by sync,
    :func:`deliver_synced_crosstalk` does the same.
    """

    set_id_suffix = FLEET_CROSSTALK_SUFFIX
    schema_revision = 1


async def deliver_synced_crosstalk(
    addresses: Iterable[Any] = (),
    *,
    gap: bool = False,
    read_row: Callable[[str], dict | None] | None = None,
    session_exists: Callable[[str], bool] | None = None,
    session_send: Callable[[str, str], Awaitable[None]] | None = None,
) -> int:
    """Deliver FleetCrosstalk rows that just arrived through sync.

    Called (event-based) from the sync scheduler's post-materialization hook
    with the addresses it just applied. For each address in THIS set only, read
    the replicated row and, when its target session is local and live, paste the
    body in. Returns the number delivered. Dependencies are injected so the
    logic is unit-testable without the live stack; production defaults resolve
    the real Settings read + tmux delivery lazily.
    """
    if gap:
        # A gap hint carries no addresses — nothing specific to deliver. The
        # next concrete materialization for this set will carry the row.
        return 0
    addresses = [a for a in addresses if getattr(a, "set_id", None) == FLEET_CROSSTALK_SET_ID]
    if not addresses:
        return 0

    if read_row is None:
        from tools.graph import settings_ops

        def read_row(key: str) -> dict | None:
            return settings_ops.read_set_key(
                FLEET_CROSSTALK_SET_ID, key, org=_PERSONAL,
            )

    if session_exists is None:
        from tools.dashboard.server import _tmux_session_exists as session_exists
    if session_send is None:
        from tools.dashboard.tmux_send import tmux_send as session_send

    delivered = 0
    for addr in addresses:
        key = getattr(addr, "key", None)
        try:
            row = read_row(key)
        except Exception:
            logger.warning("fleet crosstalk: read failed key=%s", key, exc_info=True)
            continue
        payload = (row or {}).get("payload") or {}
        target = payload.get("target_session")
        body = payload.get("body")
        if not target or not body:
            continue
        try:
            if not session_exists(target):
                # Not our session — another node owns it and will deliver.
                continue
            await session_send(target, body)
            delivered += 1
            logger.info(
                "fleet crosstalk: delivered synced message to %s (%d bytes) from %s",
                target, len(body), payload.get("sender", "?"),
            )
        except Exception:
            logger.warning(
                "fleet crosstalk: deliver failed target=%s", target, exc_info=True,
            )
    return delivered
