"""The signed personal reachability descriptor (auto-ekwbp).

One artefact with two projections (contract ``graph://7ed8a519-356`` §3): the
durable personal Settings row owned by ``auto-iipt7``, and the first-contact
cache served by the registry's reachability endpoint. **The registry does not
re-sign it** — it stores and returns the bytes it was given, and the caller
verifies the embedded machine signature itself. That is what lets one artefact
serve first contact and steady state without either projection becoming an
authority over the other.

Three rules from the contract shape everything here:

**§1 — reachability is never authority.** A verified signature says only that
the named machine published this. Whether that machine is a fleet member is the
roster's answer and is checked separately. :func:`verify` deliberately does not
take a roster and cannot admit anybody.

**§4 — durable identity and serving slot are different keys.** The row key and
the signing key are the durable roster ``machine_pub``. The optional relay
locator names a ``serving_machine_pub``, which after ``76d61b5`` genuinely
differs in production. Carrying the serving key as the row key would import
slot identity into membership.

**§6 — a missing relay locator does not make a peer unreachable.** It makes the
relay-fallback and pre-ICE paths unavailable. Direct addresses stand alone.

The ``generation`` field is a v4 amendment to §3, which lists it inside the
locator tuple: the locator is optional, so a generation living only there would
leave a locator-less machine with no ordering across a restart at all. It is a
field of the descriptor; the locator inherits it by construction.
"""

from __future__ import annotations

import threading
import time
from typing import Iterable, Mapping, Sequence

from tools.network.idkit import KeyPair, canonical_json, verify_signature

#: Domain-separated so a descriptor signature can never be replayed as any
#: other machine-key signature, and vice versa.
DESCRIPTOR_DOMAIN = b"autonomy.network.fleet-reachability.descriptor.v1\n"

DESCRIPTOR_VERSION = 1

#: One row: this machine has one identity and therefore one counter.
_GENERATION_KEY = "self"
_generation_lock = threading.RLock()

#: Bounded because this rides an announce payload and a Settings row. A machine
#: with more candidates than this publishes its best; it does not publish a
#: list that grows with its interfaces.
MAX_ADDRESSES = 8
MAX_CAPABILITIES = 16

_LOCATOR_FIELDS = frozenset({
    "relay_base", "org_uuid", "persona_pub", "serving_machine_pub",
    "capabilities", "expires_at_ns",
})


class DescriptorError(ValueError):
    """A descriptor could not be built, parsed, or verified."""


def _hex64(value: object, what: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise DescriptorError(f"{what} must be 64 lowercase hex characters")
    return value


def publishable_addresses(addrs: Iterable[str]) -> list[str]:
    """The addresses this machine advertises: deduped and bounded, nothing more.

    There is deliberately NO filter here, and the reasoning is worth keeping
    because two plausible ones were tried and both are wrong.

    Filtering by address SHAPE — drop anything RFC1918 on the theory that a
    container-bridge address means something else on the receiver's network —
    assumes the subnet collision. The daemon says otherwise: each host pins
    ``AUTONOMY_SUBNET`` from its own read-only preflight, so similar hosts
    converge (both of ours landed on 172.16.0.0/24 independently) but nothing
    guarantees it. "A design that ASSUMES collision is wrong; a design that
    assumes NON-collision is wrong more often." A shape rule also drops working
    direct paths to prevent a hazard it cannot reliably detect, which is the
    same argument ``fleet_candidate_order`` makes from the dialing end.

    Filtering out this machine's OWN addresses is worse: they are the only
    addresses it has to advertise. Declining to publish them is declining to be
    reachable.

    The real hazard is a SELF-REACHABLE address, and it is a property of
    DIALING, not of publishing: a peer's advertised address that, on this
    machine's network, reaches this machine. That check is exact, needs no
    assumption about subnets, and belongs where the dial happens. See the §6
    amendment on the contract (graph://7ed8a519-356).
    """
    out: list[str] = []
    for addr in addrs or ():
        if not isinstance(addr, str) or not addr:
            continue
        if addr in out:
            continue
        out.append(addr)
        if len(out) >= MAX_ADDRESSES:
            break
    return out


def _validated_locator(locator: Mapping | None) -> dict | None:
    if locator is None:
        return None
    if not isinstance(locator, Mapping) or set(locator) - _LOCATOR_FIELDS:
        raise DescriptorError(
            "relay locator fields must be exactly "
            f"{sorted(_LOCATOR_FIELDS)} or a subset"
        )
    relay_base = locator.get("relay_base")
    if not isinstance(relay_base, str) or not relay_base:
        raise DescriptorError("relay locator needs a relay_base")
    org_uuid = locator.get("org_uuid")
    if not isinstance(org_uuid, str) or not org_uuid:
        raise DescriptorError("relay locator needs an org_uuid")
    capabilities = list(locator.get("capabilities") or ())
    if len(capabilities) > MAX_CAPABILITIES or any(
        not isinstance(item, str) or not item for item in capabilities
    ):
        raise DescriptorError("relay locator capabilities must be short strings")
    expires = locator.get("expires_at_ns")
    if expires is not None and (
        isinstance(expires, bool) or not isinstance(expires, int) or expires < 0
    ):
        raise DescriptorError("relay locator expires_at_ns must be a timestamp")
    return {
        "relay_base": relay_base,
        "org_uuid": org_uuid,
        "persona_pub": _hex64(locator.get("persona_pub"), "persona_pub"),
        # NOT the row key. §4: the serving slot is a different identity, and
        # after 76d61b5 the org connectors genuinely carry a distinct one.
        "serving_machine_pub": _hex64(
            locator.get("serving_machine_pub"), "serving_machine_pub"),
        "capabilities": sorted(set(capabilities)),
        "expires_at_ns": int(expires or 0),
    }


def descriptor_core(
    *,
    machine_pub: str,
    addresses: Sequence[str],
    generation: int,
    signed_at_ns: int,
    relay: Mapping | None = None,
) -> bytes:
    """The exact bytes a descriptor's signature covers."""
    body: dict = {
        "v": DESCRIPTOR_VERSION,
        "machine_pub": machine_pub,
        "addresses": list(addresses),
        "generation": int(generation),
        "signed_at_ns": int(signed_at_ns),
    }
    if relay is not None:
        body["relay"] = relay
    return canonical_json(body)


def build(
    machine_key: KeyPair,
    *,
    machine_pub: str,
    addresses: Iterable[str],
    generation: int,
    relay: Mapping | None = None,
    signed_at_ns: int | None = None,
) -> dict:
    """Sign a descriptor with the durable roster machine key.

    ``machine_pub`` is the ROSTER identity and must be the one
    ``reachability_cert`` binds ``machine_key`` to. It is passed explicitly
    rather than derived from the key so a caller cannot quietly sign a
    descriptor for an identity the cert does not cover.
    """
    _hex64(machine_pub, "machine_pub")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise DescriptorError("generation must be a positive integer")
    published = publishable_addresses(addresses)
    locator = _validated_locator(relay)
    stamp = time.time_ns() if signed_at_ns is None else int(signed_at_ns)
    core = descriptor_core(
        machine_pub=machine_pub, addresses=published, generation=generation,
        signed_at_ns=stamp, relay=locator,
    )
    descriptor = {
        "v": DESCRIPTOR_VERSION,
        "machine_pub": machine_pub,
        "addresses": published,
        "generation": int(generation),
        "signed_at_ns": stamp,
        "signature": machine_key.sign(DESCRIPTOR_DOMAIN + core).hex(),
    }
    if locator is not None:
        descriptor["relay"] = locator
    return descriptor


def verify(descriptor: Mapping) -> dict:
    """Check the embedded signature and return the normalized descriptor.

    Verifies ONLY that the named machine signed these bytes. It does not
    consult a roster and cannot admit anyone: §1 makes membership the roster's
    answer and reachability a hint. A caller that treats a verified descriptor
    as authorization has read the wrong contract.
    """
    if not isinstance(descriptor, Mapping):
        raise DescriptorError("descriptor must be a mapping")
    if descriptor.get("v") != DESCRIPTOR_VERSION:
        raise DescriptorError(
            f"unsupported descriptor version {descriptor.get('v')!r}")
    machine_pub = _hex64(descriptor.get("machine_pub"), "machine_pub")
    addresses = descriptor.get("addresses")
    if not isinstance(addresses, list) or len(addresses) > MAX_ADDRESSES or any(
        not isinstance(addr, str) or not addr for addr in addresses
    ):
        raise DescriptorError("addresses must be a bounded list of strings")
    generation = descriptor.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise DescriptorError("generation must be a positive integer")
    signed_at = descriptor.get("signed_at_ns")
    if isinstance(signed_at, bool) or not isinstance(signed_at, int) or signed_at < 0:
        raise DescriptorError("signed_at_ns must be a timestamp")
    signature = descriptor.get("signature")
    if not isinstance(signature, str) or not signature:
        raise DescriptorError("descriptor is unsigned")
    locator = _validated_locator(descriptor.get("relay"))
    core = descriptor_core(
        machine_pub=machine_pub, addresses=list(addresses),
        generation=generation, signed_at_ns=signed_at, relay=locator,
    )
    # Against the ROW KEY, which is the machine's own durable identity: a
    # descriptor can only ever be signed by the machine it describes.
    verify_signature(machine_pub, signature, DESCRIPTOR_DOMAIN + core)
    out = {
        "v": DESCRIPTOR_VERSION,
        "machine_pub": machine_pub,
        "addresses": list(addresses),
        "generation": generation,
        "signed_at_ns": signed_at,
        "signature": signature,
    }
    if locator is not None:
        out["relay"] = locator
    return out


# ── the generation counter ────────────────────────────────────────────────
#
# A reader fences descriptors by ORDERING: newer wins. Three properties make
# that safe, and the publisher owns all three.
#
#   1. STRICTLY INCREASING ACROSS RESTARTS. The one that matters. A counter
#      that resets means a machine republishes 1 while a peer holds 7, the
#      peer rejects the CURRENT descriptor as stale, and it routes on
#      addresses that may be gone -- indefinitely, with nothing logged at
#      either end.
#   2. NEVER REUSED, even for a descriptor that failed to publish. A gap is
#      harmless; a repeat is not.
#   3. NOT WALL-CLOCK DERIVED. A clock that steps backwards reintroduces (1)
#      without a restart, and NTP correcting a machine that has been off for a
#      while is not hypothetical.


def next_generation(*, observed: int = 0, org: str = "machine") -> int:
    """Mint the next generation, consuming it before it is published.

    Persist-before-publish is what makes (2) hold without exception: the
    number is spent when it is minted, not when a publish succeeds, so a crash
    in between burns one rather than handing the same number to two different
    descriptors.

    ``observed`` self-heals (1). Pass the highest generation seen in ANY
    projection of THIS machine's own descriptor -- the registry cache or the
    replicated row -- and a counter that was rebuilt behind a surviving
    identity is repaired upward instead of republishing at 1.

    THE ONE WINDOW WHERE (1) STILL FAILS, stated rather than papered over: if
    the machine-local store is rebuilt while the identity survives AND every
    projection of this machine's descriptor is simultaneously unobservable at
    the next publish, the counter restarts. Both conditions are required;
    either alone is repaired by ``observed``. Closing it needs a copy that
    survives a machine-store rebuild, which is the replicated personal row --
    ``auto-iipt7`` -- and is a reason that bead exists.
    """
    from tools.graph import settings_ops
    from tools.graph.schemas.fleet_descriptor_generation import (
        FLEET_DESCRIPTOR_GENERATION_REVISION,
        FLEET_DESCRIPTOR_GENERATION_SET_ID,
        FleetDescriptorGenerationV1,
    )

    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
        raise DescriptorError("observed generation must be a non-negative integer")
    with _generation_lock:
        row = settings_ops.read_set_key(
            FLEET_DESCRIPTOR_GENERATION_SET_ID, _GENERATION_KEY,
            org=org, peers=[],
        )
        stored = 0
        if row is not None:
            value = (row["payload"] or {}).get("generation")
            if not isinstance(value, bool) and isinstance(value, int) and value > 0:
                stored = value
        nxt = max(stored, observed) + 1
        payload = {"generation": nxt}
        FleetDescriptorGenerationV1.validate(payload)
        settings_ops.upsert_by_key(
            FLEET_DESCRIPTOR_GENERATION_SET_ID,
            FLEET_DESCRIPTOR_GENERATION_REVISION,
            _GENERATION_KEY, payload, org=org, state="raw",
        )
        return nxt
