"""End-to-end serving validation — the dashboard probes its own link.

The only honest answer to "did this publish actually go live?" is to walk
the viewer's real path: fetch nothing on faith, connect the relay, run the
X25519 handshake pinned to the org root, and ask the tunnel for the object.
No internal proxy (a connector PID, a "connected" flag, the registry's view
of who is dialed in) proves that a *specific* token resolves to bytes over a
*working* tunnel — each of those can read green while the real path is
broken. So the final step of a link publish is this probe, and its result
is the sole source of truth for "the tunnel is down / the grant is dead /
it serves".

It issues an object **HEAD** (``{"op": "head", "v": 1}``), not a fetch: the
head runs the identical grant gate + target resolution on the serving end,
so a 200 proves the whole path — but it returns headers only, so a
hundreds-of-MB target costs one round-trip to validate, not a transfer.

The probe is a *client*; it reuses the same ``ViewerChannel`` the bootloader
reimplements in WebCrypto. It fails closed and never raises into the publish
path: a probe failure does not un-publish a minted grant, it only reports
the tunnel's real state so nothing overclaims that a link is live when it is
not.

Three outcomes, mapped to the honest viewer-side states:

* ``live`` — handshake completed and the object HEAD returned 200. The link
  serves.
* not live, ``status`` is a refusal (404) — the tunnel is UP (handshake
  succeeded) but the token resolves to nothing: not cached, revoked, or
  expired. Distinct from a dead tunnel.
* not live, ``status`` is None — the tunnel is not reachable: the connector
  is offline or the relay refused the channel. This is the "dashboard
  offline" state a viewer would see.
"""

from __future__ import annotations

import asyncio
import json

from tools.network.idkit import canonical_json


def registry_to_relay_ws(registry_url: str) -> str:
    """The relay WebSocket base for a registry's HTTP base URL.

    The relay rides the same host/authority as the registry (``/t/{org}``
    for the serving connector, ``/v1/links/{token}/channel`` for viewers);
    only the scheme changes. A URL that is already ``ws(s)`` passes through.
    """
    url = registry_url.rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def _interpret(raw: bytes) -> dict:
    """A channel HEAD response → the probe verdict."""
    header, _, _ = raw.partition(b"\n")
    try:
        meta = json.loads(header)
    except ValueError:
        meta = None
    status = meta.get("status") if isinstance(meta, dict) else None
    if status == 200:
        return {
            "live": True,
            "status": 200,
            "content_length": meta.get("content_length") if isinstance(meta, dict) else None,
            "detail": "the link serves: handshake completed and the target resolved",
        }
    # Handshake succeeded (we got a framed response), but the serving end
    # refused the token — the tunnel is up, the grant is dead. Not the same
    # failure as an unreachable tunnel, and the viewer must not be told it is.
    return {
        "live": False,
        "status": status,
        "content_length": None,
        "detail": (
            "the serving tunnel is up but the link resolves to nothing "
            "(not cached, revoked, or expired)"
        ),
    }


def _unreachable(detail: str) -> dict:
    return {"live": False, "status": None, "content_length": None, "detail": detail}


async def probe_link(
    *,
    relay_url: str,
    token: str,
    root_pub: str,
    org_uuid: str,
    attempts: int = 2,
    total_timeout: float = 8.0,
    connect_timeout: float = 4.0,
    read_timeout: float = 4.0,
    retry_backoff: float = 0.3,
) -> dict:
    """Prove — or disprove — that *token* actually serves over the tunnel.

    Connects as a viewer pinned to *root_pub* / *org_uuid* (the values the
    publisher already holds from its own binding, so no envelope round-trip
    is needed), issues an object HEAD, and interprets the reply.

    This is a single honest snapshot of the tunnel, not a heal-wait: a down
    tunnel self-heals in the backend and the publish reports it rather than
    blocking on it. So it makes only a small, bounded number of *attempts*
    (enough to absorb a connector that is dialing in right now), capped by a
    hard *total_timeout* ceiling for the case where something accepts the
    channel but never answers. Never raises: any transport or protocol
    failure becomes an honest "not reachable" result.
    """
    from tools.network.relaykit.viewer import ViewerChannel

    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout
    result = _unreachable("the serving tunnel did not respond")

    for attempt in range(max(1, attempts)):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            channel = await ViewerChannel.connect(
                relay_url, token, root_pub=root_pub, org=org_uuid,
                open_timeout=min(connect_timeout, remaining),
            )
        except Exception as exc:  # relay refused, connector offline, DNS, TLS…
            result = _unreachable(
                "the serving tunnel is not reachable "
                f"(connector offline or relay refused the channel): {exc}"
            )
        else:
            try:
                async with channel:
                    await channel.send_message(canonical_json({"op": "head", "v": 1}))
                    budget = min(read_timeout, max(deadline - loop.time(), 0.1))
                    raw = await asyncio.wait_for(channel.recv_message(), timeout=budget)
                return _interpret(raw)
            except asyncio.TimeoutError:
                result = _unreachable(
                    "the serving tunnel accepted the channel but did not "
                    "answer the probe in time"
                )
            except Exception as exc:
                result = _unreachable(f"the serving channel failed mid-probe: {exc}")

        # A short backoff before the next attempt only to catch a connector
        # dialing in at this instant; not a wait for the backend to heal.
        if attempt + 1 < attempts and deadline - loop.time() > retry_backoff:
            await asyncio.sleep(retry_backoff)

    return result
