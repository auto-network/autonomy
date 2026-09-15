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

It issues an object **HEAD** (``{"op": "head", "v": 1}``), not a fetch. The
head runs the identical grant gate, resolution, serialization, and size check
on the serving end. A wire ``status:"ok"`` therefore proves the whole path,
but the response contains only ``serialized_size`` and transfers no artifact.

The probe is a *client*; it reuses the same ``ViewerChannel`` the bootloader
reimplements in WebCrypto. It fails closed and never raises into the publish
path: a probe failure does not un-publish a minted grant, it only reports
the tunnel's real state so nothing overclaims that a link is live when it is
not.

Three outcomes, mapped to the honest viewer-side states:

* ``live`` — handshake completed and the object HEAD returned ``"ok"``. The
  public verdict retains HTTP-like ``status:200`` for dashboard callers.
* not live, ``status`` is a refusal (404 in the public verdict) — the tunnel is UP (handshake
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
    wire_status = meta.get("status") if isinstance(meta, dict) else None
    if wire_status == "ok" and meta.get("v") == 1:
        return {
            "live": True,
            "status": 200,
            "content_length": meta.get("serialized_size"),
            "detail": "the link serves: handshake completed and the target resolved",
        }
    # Handshake succeeded (we got a framed response), but the serving end
    # refused the token — the tunnel is up, the grant is dead. Not the same
    # failure as an unreachable tunnel, and the viewer must not be told it is.
    return {
        "live": False,
        "status": 404 if wire_status == "unavailable" else None,
        "content_length": None,
        "detail": (
            "the serving tunnel is up but the link resolves to nothing "
            "(not cached, revoked, or expired)"
        ),
    }


def _unreachable(detail: str, *, close_code: int | None = None,
                 close_reason: str | None = None) -> dict:
    out = {"live": False, "status": None, "content_length": None, "detail": detail}
    if close_code is not None:
        out["close_code"] = close_code
        out["close_reason"] = close_reason or ""
    return out


def _closed(exc: BaseException, stage: str) -> dict:
    """A close from the far side, named: the code, its meaning and the
    remedy (relaykit.close_codes), never a bare '1000 (OK)'."""
    from tools.network.relaykit.close_codes import describe

    rcvd = getattr(exc, "rcvd", None)
    code = getattr(rcvd, "code", None)
    reason = getattr(rcvd, "reason", None) or ""
    return _unreachable(f"{stage}: {describe(code, reason)}",
                        close_code=code, close_reason=reason)


async def _attempt_once(relay_url, token, link_pub, org_uuid, operation,
                        connect_timeout):
    """One probe attempt with NO internal wall — the caller wraps it in a
    single ``asyncio.wait_for`` so the whole thing is bounded.

    This is deliberate: ``ViewerChannel.connect`` awaits the SERVER_HELLO
    with no timeout of its own (``open_timeout`` only bounds the WS upgrade),
    so a relay that accepts the socket but never completes the handshake —
    the exact shape of a reachable registry with no serving tunnel dialed in
    — would otherwise block forever. The outer ``wait_for`` cancels this
    coroutine on the deadline, and ``connect``/``__aexit__`` close the socket.
    """
    from tools.network.relaykit.viewer import ViewerChannel

    from websockets.exceptions import ConnectionClosed

    try:
        channel = await ViewerChannel.connect(
            relay_url, token, link_pub=link_pub,
            org=org_uuid, open_timeout=connect_timeout,
        )
    except ConnectionClosed as exc:
        return _closed(exc, "the far side closed the channel before it served")
    except Exception as exc:  # DNS, TLS, refused socket, handshake shape…
        return _unreachable(
            "the serving tunnel is not reachable "
            f"(connector offline or relay refused the channel): {exc}"
        )
    try:
        async with channel:
            await channel.send_message(canonical_json({"op": operation, "v": 1}))
            raw = await channel.recv_message()
        return _interpret(raw)
    except ConnectionClosed as exc:
        return _closed(exc, "the far side closed the channel mid-probe")
    except Exception as exc:
        return _unreachable(f"the serving channel failed mid-probe: {exc}")


async def probe_link(
    *,
    relay_url: str,
    token: str,
    link_pub: str,
    org_uuid: str,
    operation: str,
    attempts: int = 2,
    total_timeout: float = 6.0,
    connect_timeout: float = 3.0,
    retry_backoff: float = 0.3,
) -> dict:
    """Prove — or disprove — that *token* actually serves over the tunnel.

    Connects as a viewer pinned to the per-link fragment public key, issues
    the explicitly selected ``head`` or ``context`` operation, and interprets
    the reply.

    This is a single honest snapshot of the tunnel, not a heal-wait: a down
    tunnel self-heals in the backend and the publish reports it rather than
    blocking on it. So it makes only a small, bounded number of *attempts*
    (enough to absorb a connector that is dialing in right now). Each attempt
    is wrapped in a hard ``asyncio.wait_for`` so a relay that accepts the
    socket but never completes the handshake CANNOT hang the publish — the
    whole probe is bounded by *total_timeout*. Never raises: any transport,
    protocol, or timeout failure becomes an honest "not reachable" result.
    """
    if operation not in {"head", "context"}:
        raise ValueError("link probe operation must be 'head' or 'context'")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout
    result = _unreachable("the serving tunnel did not respond within the probe budget")

    for attempt in range(max(1, attempts)):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            result = await asyncio.wait_for(
                _attempt_once(relay_url, token, link_pub, org_uuid, operation,
                              min(connect_timeout, remaining)),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            result = _unreachable(
                "the serving tunnel did not complete the probe in time "
                "(reachable but no serving connector answered)"
            )
        except Exception as exc:
            result = _unreachable(f"the serving probe failed: {exc}")

        # A definitive verdict — served (live) or refused (grant dead, 404) —
        # ends the probe. Only an unreachable/timeout result (status None) is
        # worth another attempt, to catch a connector dialing in right now.
        if result["live"] or result.get("status") is not None:
            return result
        if attempt + 1 < attempts and deadline - loop.time() > retry_backoff:
            await asyncio.sleep(retry_backoff)

    return result
