"""Remote access from another machine of the fleet (bead auto-fnj20).

A lost or revoked gate passkey must not lock the operator out of a machine
they can no longer reach at its relay address. Re-opening enrollment and
revoking a passkey are therefore accepted from two places only: the owning
machine's local listener, and another ACTIVE machine of the same fleet.
Never through the gated relay route.

The fleet path reuses session-control/1 (tools/dashboard/session_control_client):
the caller's dashboard sends one op to the owning machine over the fleet
directed stream, and the owning machine decides on ``peer_machine_pub`` --
the machine the session-control handshake proved -- never on a field of
the request body. The peer is recorded as the opener of the enrollment it
minted.

* :func:`ops` -- the inbound handlers, registered at worker activation
  beside ``launch``/``send``/``stop``.
* :func:`forward` -- the caller's side: the remote-access routes hand a
  request naming another machine here and return its reply.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

STATUS_OP = "remote-access.status"
OPEN_OP = "remote-access.enrollment.open"
CLOSE_OP = "remote-access.enrollment.close"
REVOKE_OP = "remote-access.revoke"

#: A peer waits this long for the owning machine's answer.
TIMEOUT_S = 15.0


def _peer_name(peer_machine_pub: str) -> str:
    return f"fleet:{(peer_machine_pub or '')[:12]}"


# ── inbound: this machine answers a fleet peer ────────────────────────────


async def _status(_body: dict, _peer: str) -> dict:
    from tools.dashboard import remote_access, session_control_client

    status = await remote_access.status(enrollment_link=True)
    return session_control_client.ok({"status": status})


async def _open(_body: dict, peer_machine_pub: str) -> dict:
    from tools.dashboard import passkey_gate, remote_access, session_control_client

    recorded = remote_access.current() or {}
    if recorded.get("mode") != "autonomy":
        return session_control_client.refusal("not_published", "this machine is not published on the relay")
    minted = await asyncio.to_thread(
        passkey_gate.open_enrollment, opened_by=_peer_name(peer_machine_pub))
    logger.info("gate enrollment opened by fleet peer %s", peer_machine_pub[:12])
    return session_control_client.ok({
        "enrollment_url": f"{recorded['origin']}/oauth2/enroll?token={minted['token']}",
        "expires_at": minted["expires_at"],
    })


async def _close(_body: dict, peer_machine_pub: str) -> dict:
    from tools.dashboard import passkey_gate, session_control_client

    await asyncio.to_thread(passkey_gate.close_enrollment)
    logger.info("gate enrollment closed by fleet peer %s", peer_machine_pub[:12])
    return session_control_client.ok({})


async def _revoke(body: dict, peer_machine_pub: str) -> dict:
    from tools.dashboard import passkey_gate, session_control_client

    credential_id = body.get("credential_id") if isinstance(body, dict) else None
    if not isinstance(credential_id, str) or not credential_id:
        return session_control_client.refusal("invalid_credential", "credential_id is required")
    try:
        saved = await asyncio.to_thread(passkey_gate.revoke_credential, credential_id)
    except passkey_gate.GateRefusal as exc:
        return session_control_client.refusal(exc.code)
    logger.info("gate passkey %s revoked by fleet peer %s", credential_id[:12], peer_machine_pub[:12])
    return session_control_client.ok({"enrolled": len(saved.get("credentials") or [])})


def ops() -> dict:
    """The inbound op table entries, for session_control_client.install."""
    return {STATUS_OP: _status, OPEN_OP: _open, CLOSE_OP: _close, REVOKE_OP: _revoke}


# ── outbound: this machine asks the owning machine ────────────────────────


def is_this_machine(machine: str | None) -> bool | None:
    """True when *machine* names this machine, False when it names another
    active fleet machine, None when it names nothing this fleet knows."""
    from tools.dashboard import session_control_client, session_presence

    if not machine:
        return True
    local = session_presence.local_machine()
    if local is not None and machine == local.machine_pub:
        return True
    pub = session_control_client.resolve_machine(machine)
    if pub is None:
        return None
    return local is not None and pub == local.machine_pub


async def forward(machine: str, op: str, body: dict | None = None) -> dict:
    """Send *op* to the fleet machine *machine*; its reply record."""
    from tools.dashboard import session_control_client

    return await session_control_client.request(machine, op, body or {}, timeout=TIMEOUT_S)
