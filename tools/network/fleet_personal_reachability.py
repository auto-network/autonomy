"""Publish and verify one personal-fleet machine's reachability descriptor.

The personal-fleet counterpart of :mod:`tools.network.fleet_org_reachability`
(auto-iipt7). A machine writes ONE row, keyed by its durable roster public key
and signed by its own machine key, only when its semantic tuple changes. Peers
read it as a DISCOVERY HINT: the durable personal roster remains the authority
on who is a member, and a descriptor that cannot be bound to an active roster
machine yields nothing.

Scope of this module is deliberately schema, writer and verifier. It performs no
registry publication, no enrollment change, no supervisor or selector change.

TRUSTED INPUTS, stated because this module verifies against them and does not
establish them. ``machine_key`` and ``reachability_cert`` are supplied together
by an authorized runtime payload (``FleetRuntimeCredential``), which is where
that pairing is validated; absence here means publication is unavailable, not
that authority is checked here. ``active_machine_pubs`` is a RESOLVED roster
supplied by the caller; this module does not resolve, refresh or re-derive it,
and adding a second roster path here would create a competing authority.

Two rules here are load-bearing and easy to lose:

* **Address order is signed candidate priority.** Deduplication preserves first
  occurrence and nothing is ever sorted. Sorting would erase the sender's
  intended rank while still verifying.
* **Untrusted input is never normalized before verification.** A verifier that
  deduplicated, truncated, re-ordered or rewrote a received row and then checked
  the signature would be verifying a body the signer never produced. Received
  rows must ALREADY be canonical; if they are not, they are refused.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

from tools.graph import settings_ops
from tools.graph.schemas.personal_fleet_reachability import (
    MAX_ADDRESSES,
    MAX_ADDRESS_BYTES,
    MAX_RELAY_BASE_BYTES,
    MAX_ROW_BYTES,
    PERSONAL_FLEET_REACHABILITY_REVISION,
    PERSONAL_FLEET_REACHABILITY_SET_ID,
    ROW_VERSION,
    PersonalFleetReachabilityV1,
    row_bytes,
)
from tools.graph.schemas.registry import SchemaValidationError
from tools.network.idkit import KeyPair
from tools.network.idkit.keys import verify_signature
from tools.network.idkit.canonical import canonical_json

logger = logging.getLogger(__name__)

ROW_DOMAIN = b"autonomy.network.fleet-personal-reachability.row.v1\n"

#: Raw input ceilings applied BEFORE any parsing, normalization, encoding or
#: signature work, so a hostile input cannot make us do expensive things first.
_MAX_RAW_INPUT_ADDRESSES = 64
_MAX_RAW_INPUT_BYTES = 16384


class ReachabilityUnavailable(RuntimeError):
    """Publication is not possible because signing authority is absent.

    Raised when the machine key or its paired reachability certificate has not
    been supplied by an authorized payload. It is NOT an error condition to
    repair locally: the last valid row is preserved and nothing is signed with a
    process delegate or any fabricated cold authority.
    """


def _signing_input(body: dict[str, Any]) -> bytes:
    return ROW_DOMAIN + canonical_json({k: v for k, v in body.items() if k != "sig"})


def _raw_bounds(addresses: Sequence[str], relay: dict | None) -> None:
    """Cheap hard bounds on caller input, before any URL parsing."""
    if not isinstance(addresses, (list, tuple)):
        raise SchemaValidationError("addresses must be a list")
    if len(addresses) > _MAX_RAW_INPUT_ADDRESSES:
        raise SchemaValidationError("addresses input exceeds the raw ceiling")
    total = 0
    for address in addresses:
        if not isinstance(address, str):
            raise SchemaValidationError("every address must be a string")
        total += len(address.encode("utf-8"))
    if relay is not None:
        # Validate the PRIMITIVE shape and lengths before canonicalizing, so an
        # arbitrary nested object cannot be serialized just to measure it.
        if not isinstance(relay, dict) or len(relay) > 8:
            raise SchemaValidationError("relay must be a small object or None")
        for key, value in relay.items():
            if not isinstance(key, str) or len(key) > 32:
                raise SchemaValidationError("relay keys must be short strings")
            if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_RELAY_BASE_BYTES:
                raise SchemaValidationError("relay values must be bounded strings")
            total += len(key.encode("utf-8")) + len(value.encode("utf-8"))
    if total > _MAX_RAW_INPUT_BYTES:
        raise SchemaValidationError("reachability input exceeds the raw byte ceiling")


#: A fixed-length stand-in so the body can be validated — bounds included —
#: BEFORE any real signing happens. The signature is always 128 hex chars, so
#: the placeholder measures exactly what the signed row will measure.
_SIG_PLACEHOLDER = "0" * 128


def normalize_address(address: str) -> str:
    """Canonical spelling of one direct address.

    Shared by the writer (which dedupes on it) and required by the verifier
    (which refuses a received address that is not already in this form).
    Without one shared definition `wss://Host:443/s` and `wss://host/s` are two
    raw spellings of one endpoint: the writer would keep both as distinct
    candidates and the verifier would accept either.

    Scheme and host are lowercased and a default port is dropped. PATH AND
    QUERY ARE PRESERVED — they address a listener, not decoration — and so is
    position, which is candidate preference.
    """
    if not isinstance(address, str) or not address:
        raise SchemaValidationError("address must be a non-empty string")
    try:
        split = urlsplit(address)
        port = split.port
        host = split.hostname
    except ValueError as exc:
        raise SchemaValidationError(f"address is malformed: {exc}") from exc
    if split.scheme not in ("ws", "wss"):
        raise SchemaValidationError("address must be ws:// or wss://")
    if not host:
        raise SchemaValidationError("address must have a host")
    if split.username or split.password:
        raise SchemaValidationError("address must not carry userinfo")
    if split.fragment:
        raise SchemaValidationError("address must not carry a fragment")
    if ":" in host:
        host = f"[{host}]"
    default = 80 if split.scheme == "ws" else 443
    authority = host if port in (None, default) else f"{host}:{port}"
    out = f"{split.scheme}://{authority}{split.path}"
    if split.query:
        out = f"{out}?{split.query}"
    return out


def canonical_addresses(addresses: Iterable[str]) -> list[str]:
    """Normalize WRITER-SUPPLIED addresses: validate, dedupe, cap.

    Deduplication preserves FIRST OCCURRENCE and the list is never sorted —
    order is the signed candidate priority. Applied only to the local machine's
    own input; never to a received row.
    """
    out: list[str] = []
    for address in addresses:
        if not isinstance(address, str) or not address:
            continue
        if len(address.encode("utf-8")) > MAX_ADDRESS_BYTES:
            continue
        try:
            address = normalize_address(address)
        except SchemaValidationError:
            continue
        if address in out:  # dedupe on the NORMALIZED spelling, not the raw one
            continue
        out.append(address)
        if len(out) == MAX_ADDRESSES:
            break
    return out


def canonical_relay_origin(relay_base: str) -> str:
    """Normalize a relay ORIGIN, rejecting anything that is not one.

    A relay base names an origin the caller is already configured to use, not an
    arbitrary dial target chosen by a descriptor. Userinfo, a non-root path, a
    query and a fragment are all refused rather than stripped: stripping would
    silently accept a row whose signer meant something else.

    The canonical form omits a default port and re-brackets an IPv6 host, so
    ``wss://[::1]:443`` and ``wss://[::1]`` are one value rather than two that
    compare unequal while naming the same endpoint.
    """
    if not isinstance(relay_base, str) or not relay_base:
        raise SchemaValidationError("relay_base must be a non-empty string")
    try:
        split = urlsplit(relay_base)
        port = split.port
        host = split.hostname
    except ValueError as exc:
        # urlsplit and .port raise on a malformed authority; a signed row must
        # not be able to escape verification through an uncaught exception.
        raise SchemaValidationError(f"relay_base is malformed: {exc}") from exc
    if split.scheme not in ("https", "wss"):
        raise SchemaValidationError("relay_base must be an https:// or wss:// origin")
    if split.username or split.password:
        raise SchemaValidationError("relay_base must not carry userinfo")
    if split.path not in ("", "/") or split.query or split.fragment:
        raise SchemaValidationError("relay_base must be a bare origin")
    if not host:
        raise SchemaValidationError("relay_base must have a host")
    if ":" in host:  # IPv6 literal — urlsplit strips the brackets
        host = f"[{host}]"
    origin = f"{split.scheme}://{host}"
    if port is not None and port != 443:
        origin = f"{origin}:{port}"
    if len(origin.encode("utf-8")) > MAX_RELAY_BASE_BYTES:
        raise SchemaValidationError("relay_base exceeds its byte ceiling")
    return origin


def semantic_tuple(row: dict[str, Any]) -> tuple:
    """The full comparison key for change detection.

    Addresses AND relay, including relay becoming absent. Comparing addresses
    alone would miss a changed serving slot, which is a real route change.
    ``updated_at`` and ``sig`` are excluded so they can neither mask a real
    difference nor manufacture a false one.
    """
    relay = row.get("relay")
    return (
        tuple(row.get("addresses") or ()),
        None if relay is None else (
            relay.get("relay_base"), relay.get("org_uuid"),
            relay.get("persona_pub"), relay.get("serving_machine_pub"),
        ),
    )


def build_row(
    machine_key: KeyPair,
    addresses: Sequence[str],
    relay: dict | None = None,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    """This machine's signed descriptor. Raises on a shape it must not sign."""
    _raw_bounds(addresses, relay)
    body: dict[str, Any] = {
        "v": ROW_VERSION,
        "machine_pub": machine_key.public_hex,
        "addresses": canonical_addresses(addresses),
        "relay": None if relay is None else {
            "relay_base": canonical_relay_origin(str(relay.get("relay_base", ""))),
            "org_uuid": relay.get("org_uuid"),
            "persona_pub": relay.get("persona_pub"),
            "serving_machine_pub": relay.get("serving_machine_pub"),
        },
        "updated_at": int(time.time() if now is None else now),
    }
    # Validate the complete bounded body BEFORE signing. Signing first and
    # validating after would still raise, but it would have already performed a
    # private-key operation on input we had not accepted.
    body["sig"] = _SIG_PLACEHOLDER
    PersonalFleetReachabilityV1.validate(body)
    body["sig"] = machine_key.sign_hex(_signing_input(body))
    return body


def stored_row(machine_pub: str) -> dict[str, Any] | None:
    """This machine's last stored row, or None."""
    try:
        member = settings_ops.read_set_key(
            PERSONAL_FLEET_REACHABILITY_SET_ID, machine_pub, org="personal")
    except Exception:
        return None
    if member is None:
        return None
    payload = member.get("payload") if isinstance(member, dict) else None
    return payload if isinstance(payload, dict) else None


def publish_if_changed(
    machine_key: KeyPair | None,
    reachability_cert: Any,
    addresses: Sequence[str],
    relay: dict | None = None,
    *,
    now: int | None = None,
) -> bool:
    """Write this machine's row ONLY when its semantic tuple changed.

    Returns True when a row was written. An unchanged tuple mutates nothing —
    no Settings write, no ``updated_at`` churn — because this row is
    change-driven and never a heartbeat.

    Removal is a live row with ``addresses=[]`` and ``relay=None``, not a hard
    delete, so a machine that stopped listening stops being dialled while its
    statement of that fact remains verifiable.
    """
    if machine_key is None or reachability_cert is None:
        raise ReachabilityUnavailable(
            "machine key and reachability certificate are required to publish; "
            "the last valid row is preserved"
        )
    wanted = build_row(machine_key, addresses, relay, now=now)
    # The baseline is the last VERIFIED own row, not merely the last stored one.
    # A corrupt or tampered stored row must cause a republish, never suppress
    # one: comparing against something unverified could hold back a real change.
    stored = verify_own_row(stored_row(machine_key.public_hex), machine_key.public_hex)
    if stored is not None and semantic_tuple(stored) == semantic_tuple(wanted):
        return False
    settings_ops.upsert_by_key(
        PERSONAL_FLEET_REACHABILITY_SET_ID,
        PERSONAL_FLEET_REACHABILITY_REVISION,
        machine_key.public_hex,
        wanted,
        org="personal",
    )
    return True


def verify_own_row(row: Any, machine_pub: str) -> dict[str, Any] | None:
    """Integrity check of THIS machine's own stored row, for change detection.

    Deliberately NOT :func:`verify_row`. That function answers "is this peer's
    descriptor usable here", and fails closed without local relay context — the
    right answer for a peer, and the wrong one for our own baseline, where
    absent context would make every relay-bearing row look unverifiable and
    republish on every call, destroying the change-only property.

    This answers the narrower question the writer actually asks: is the stored
    row a well-formed row that THIS machine signed? Shape, row key and
    signature; no relay binding, because the writer is not deciding usability.
    """
    if row is None:
        return None
    try:
        PersonalFleetReachabilityV1.validate(row)
    except (SchemaValidationError, UnicodeError, ValueError, TypeError):
        # Same malformed-value boundary as the peer verifier. A stored own row
        # carrying malformed Unicode or an unencodable integer must be REJECTED
        # and republished, never allowed to abort publication.
        return None
    if row.get("machine_pub") != machine_pub:
        return None
    try:
        verify_signature(machine_pub, row["sig"], _signing_input(row))
    except Exception:
        return None
    return row


def verify_row(
    row: Any,
    member_key: str,
    *,
    active_machine_pubs: Iterable[str],
    configured_relay_origin: str | None = None,
    configured_org_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Return *row* when it is usable as a hint from an active roster machine.

    Returns None on every failure, and a caller must treat None as "no
    descriptor" — never as licence to replace a previously verified value.

    The row is checked AS RECEIVED. Nothing here deduplicates, re-orders,
    truncates or rewrites the input before checking the signature: the schema
    requires the received body to be already canonical, so a non-canonical row
    is refused rather than repaired into one that verifies.
    """
    try:
        PersonalFleetReachabilityV1.validate(row)
        if row_bytes(row) > MAX_ROW_BYTES:
            return None
    except (SchemaValidationError, UnicodeError, ValueError, TypeError):
        # Malformed Unicode or a failed value conversion is INVALID INPUT, not
        # an exception for a caller to handle. Anything that cannot be
        # canonicalized cannot have been signed in this form.
        return None

    # Received addresses must ALREADY be canonical. Normalizing them here and
    # then verifying would check a body the signer never produced, and accepting
    # un-normalized spellings would let one endpoint appear as several
    # candidates.
    try:
        for address in row["addresses"]:
            if address != normalize_address(address):
                return None
    except (SchemaValidationError, UnicodeError, ValueError, TypeError):
        return None

    machine_pub = row["machine_pub"]
    # The row key and the signing key must be the SAME durable machine, or one
    # member could publish another member's location under their key.
    if member_key != machine_pub:
        return None
    if machine_pub not in set(active_machine_pubs):
        return None

    try:
        # Raises on mismatch or malformed input; returns None on success.
        verify_signature(machine_pub, row["sig"], _signing_input(row))
    except Exception:
        return None

    relay = row.get("relay")
    if relay is not None:
        # FAIL CLOSED. A relay-bearing row is only usable when this node has the
        # local context to bind it. Treating absent context as "skip the check"
        # would return a signed locator as usable without ever confirming it
        # names a relay and org this node agreed to — which is the whole point
        # of binding it.
        if configured_relay_origin is None or configured_org_uuid is None:
            return None
        try:
            received = relay["relay_base"]
            # The RECEIVED value must already be canonical. Canonicalizing it
            # and then comparing would accept a body the signer never produced.
            if received != canonical_relay_origin(received):
                return None
            if canonical_relay_origin(received) != canonical_relay_origin(
                    configured_relay_origin):
                return None
        except SchemaValidationError:
            return None
        if relay["org_uuid"] != configured_org_uuid:
            return None
    return row
