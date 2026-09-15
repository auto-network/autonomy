"""Every application WebSocket close code a viewer can receive, in one table,
each with what it means and what to do about it.

Two families, two authors:

* **44xx — the RELAY closed the viewer.** These are deliberately coarse on
  the wire: an anonymous viewer must not be able to tell an unknown token
  from a live link whose connector is offline, so the relay says 4404 for
  both and logs the distinction on its own side.
* **45xx — the CONNECTOR refused the channel.** By the time a viewer reaches
  a connector, the relay has already routed a live token to a live tunnel,
  so the connector can be exact without disclosing anything about token
  validity. Before this family existed the connector's refusal travelled as
  an EMPTY close frame and the relay translated it to 1000 ("normal
  closure"): the one close that meant "the connector would not serve you"
  was the one that said nothing (dynbench, 2026-09-15). The connector now
  sends the code and its own words in the close frame; the relay forwards
  them to the viewer verbatim.

The wire shape of a coded close frame payload is ``[2-byte big-endian
code][UTF-8 reason]``. An empty payload (a connector that predates this)
still closes with 1000, so the two generations interoperate.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── relay-authored ────────────────────────────────────────────────────────
CLOSE_UNAUTHENTICATED = 4403
CLOSE_UNKNOWN_LINK = 4404
CLOSE_PROTOCOL_MISMATCH = 4406
CLOSE_REPLACED = 4409
CLOSE_VIEWER_QUEUE_OVERFLOW = 4413
CLOSE_LISTENER_FELL_BEHIND = 4416
CLOSE_MEMBERSHIP_STALE = 4417

# ── connector-authored ────────────────────────────────────────────────────
#: The connector hit an error it does not classify; its log names it.
CLOSE_CONNECTOR_ERROR = 4500
#: The connector holds no process credential: it is UNARMED. Nothing it
#: serves can resolve a key until the dashboard re-arms it.
CLOSE_CONNECTOR_UNARMED = 4501
#: The dashboard's link-key resolver refused this link. The dashboard log
#: names which check failed ("link key resolution refused for link ...").
CLOSE_KEY_RESOLUTION_REFUSED = 4502
#: The connector was launched with no resolver client at all.
CLOSE_AUTHORIZATION_UNAVAILABLE = 4503
#: The viewer's handshake or a record failed to open: the link URL's
#: fragment key does not match the key the connector holds, or the
#: channel was corrupted.
CLOSE_VIEWER_HANDSHAKE_FAILED = 4504

CONNECTOR_CODE_RANGE = range(4500, 4600)
MAX_REASON_BYTES = 200


@dataclass(frozen=True)
class CloseMeaning:
    name: str
    meaning: str
    remedy: str


MEANINGS: dict[int, CloseMeaning] = {
    1000: CloseMeaning(
        "normal closure",
        "the far side ended the channel normally",
        "nothing, unless the channel ended before it served: then the "
        "connector predates coded closes and its log has the reason",
    ),
    1006: CloseMeaning(
        "abnormal closure",
        "the connection dropped without a close frame (network, a crashed "
        "process, a proxy timeout)",
        "check the connector's tunnel state (fleet_doctor: last disconnect) "
        "and the relay's health",
    ),
    CLOSE_UNAUTHENTICATED: CloseMeaning(
        "unauthenticated",
        "the relay refused the hello or the viewer's credential",
        "the serving credential is not accepted by the relay: sign in again "
        "so it is re-minted",
    ),
    CLOSE_UNKNOWN_LINK: CloseMeaning(
        "unknown link or no serving tunnel",
        "the relay could not route the viewer: the token is unknown, "
        "expired or revoked, or no connector for its organization (or its "
        "declared serving machine) is connected to the relay",
        "fleet_doctor: is the organization's connector SERVING? If yes, the "
        "link itself is dead: publish again",
    ),
    CLOSE_PROTOCOL_MISMATCH: CloseMeaning(
        "protocol mismatch",
        "the relay and this side speak different tunnel protocol versions",
        "deploy matching code on the registry and the node",
    ),
    CLOSE_REPLACED: CloseMeaning(
        "replaced",
        "another connector registered the same (persona, machine) slot; "
        "this connection was displaced",
        "two connectors exist for one credential: fleet_doctor lists "
        "connector processes per organization; one supervisor must own one",
    ),
    CLOSE_VIEWER_QUEUE_OVERFLOW: CloseMeaning(
        "viewer queue overflow",
        "this viewer fell too far behind the relay's per-viewer queue",
        "a slow viewer; reconnect",
    ),
    CLOSE_LISTENER_FELL_BEHIND: CloseMeaning(
        "listener fell behind",
        "a stream listener fell behind the retention window",
        "reconnect and request the offset through history",
    ),
    CLOSE_MEMBERSHIP_STALE: CloseMeaning(
        "membership proof stale",
        "the organization's verified checkpoint advanced and the connector "
        "did not re-prove membership in time",
        "the connector reconnects with a fresh proof; if it keeps happening, "
        "the node cannot build a membership commitment (fleet_doctor: live "
        "worker state, membership)",
    ),
    CLOSE_CONNECTOR_ERROR: CloseMeaning(
        "connector error",
        "the serving connector hit an error it does not classify",
        "read the connector's log (serve-<org>-*.log): 'viewer channel ... "
        "closed on an error'",
    ),
    CLOSE_CONNECTOR_UNARMED: CloseMeaning(
        "connector unarmed",
        "the serving connector holds no process credential, so it cannot "
        "resolve any link's key",
        "unlock the dashboard (or wait for the re-arm after a reload); "
        "fleet_doctor's live worker state shows armed=False until then",
    ),
    CLOSE_KEY_RESOLUTION_REFUSED: CloseMeaning(
        "link key resolution refused",
        "the dashboard refused to release this link's channel key to the "
        "connector",
        "read the dashboard log line 'link key resolution refused for link "
        "<prefix>...: <reason>'; it names which check failed",
    ),
    CLOSE_AUTHORIZATION_UNAVAILABLE: CloseMeaning(
        "channel authorization unavailable",
        "the connector was launched without a link-key resolver client",
        "the supervisor launched it wrong; restart the connector",
    ),
    CLOSE_VIEWER_HANDSHAKE_FAILED: CloseMeaning(
        "viewer handshake failed",
        "the viewer's handshake or a record did not open against the key "
        "the connector holds",
        "the link URL's fragment key does not match the vaulted key: the "
        "link was re-minted, or the URL was copied wrong",
    ),
}


def encode_close_payload(code: int, reason: str = "") -> bytes:
    """The close frame payload for a connector-authored close."""
    if code not in CONNECTOR_CODE_RANGE:
        raise ValueError(f"connector close codes are {CONNECTOR_CODE_RANGE}, got {code}")
    text = (reason or "").encode("utf-8", "replace")[:MAX_REASON_BYTES]
    return code.to_bytes(2, "big") + text


def decode_close_payload(payload: bytes) -> tuple[int, str]:
    """``(code, reason)`` from a close frame payload. Empty or malformed
    (a connector that predates coded closes, or noise) is a normal 1000
    with no reason: the relay never invents a code."""
    if not payload or len(payload) < 2:
        return 1000, ""
    code = int.from_bytes(payload[:2], "big")
    if code not in CONNECTOR_CODE_RANGE:
        return 1000, ""
    reason = payload[2:2 + MAX_REASON_BYTES].decode("utf-8", "replace")
    return code, reason


def classify_connector_error(exc: BaseException) -> tuple[int, str]:
    """The close code and reason for an error that ended a viewer channel
    inside the connector. The reason is the error's own words."""
    text = str(exc) or type(exc).__name__
    if isinstance(exc, PermissionError):
        if "credential unavailable" in text:
            return CLOSE_CONNECTOR_UNARMED, text
        if "resolution refused" in text:
            return CLOSE_KEY_RESOLUTION_REFUSED, text
        if "authorization unavailable" in text:
            return CLOSE_AUTHORIZATION_UNAVAILABLE, text
        return CLOSE_KEY_RESOLUTION_REFUSED, text
    name = type(exc).__name__
    if name in ("HandshakeError", "RecordError"):
        return CLOSE_VIEWER_HANDSHAKE_FAILED, f"{name}: {text}"
    return CLOSE_CONNECTOR_ERROR, f"{name}: {text}"


def describe(code: int | None, reason: str | None = None) -> str:
    """One sentence a person can act on: the code, its name, the far side's
    words when it sent any, and the remedy."""
    if code is None:
        return "no close code (the connection did not close cleanly)"
    meaning = MEANINGS.get(code)
    head = f"close {code}" + (f" ({meaning.name})" if meaning else "")
    parts = [head]
    if reason:
        parts.append(f"the far side said: {reason}")
    if meaning:
        parts.append(meaning.meaning)
        parts.append(f"remedy: {meaning.remedy}")
    return "; ".join(parts)
