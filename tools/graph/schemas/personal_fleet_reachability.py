"""One personal-fleet machine's reachability descriptor, self-certified.

The personal-fleet analogue of :mod:`tools.graph.schemas.org_fleet_reachability`
(auto-iipt7). One row per machine, keyed by the machine's durable roster public
key, written by that machine alone and signed by its own machine key, so no
machine can publish another's location.

It is a DISCOVERY HINT, never authority. The durable personal roster decides who
is a member; this row only says where a member currently listens. A verifier
that cannot bind the row to an active roster machine returns nothing.

Deliberately absent, each for a stated reason:

* **no generation counter** — Settings ordering plus the change-only convention
  already converge this row, and a counter cannot provide replay protection once
  writer and reader state are both restored, so it would add state without
  buying the stronger property. ``updated_at`` is CONVERGENCE METADATA and must
  never be documented or relied on as anti-rollback.
* **no capabilities** — tunnel capabilities are negotiated live in the
  authenticated hello. Stale advertised support must not substitute for live
  negotiation.
* **no expiry** — the durable row is change-driven and does not expire; a route
  stays valid until its machine publishes a different descriptor or an empty
  removal. Expiry belongs to the registry first-contact cache, which has its own
  TTL, not to durable state.
"""

from __future__ import annotations

import re
from typing import Any

from tools.graph.schemas.registry import (
    SettingSchema,
    SchemaValidationError,
    home,
    keyed_per_entity,
    publication_band,
)

PERSONAL_FLEET_REACHABILITY_SET_ID = "autonomy.personal.fleet-reachability"
PERSONAL_FLEET_REACHABILITY_REVISION = 1
ROW_VERSION = 1

#: Matches org_fleet_reachability.MAX_ADDRESSES, itself the personal announce's
#: bound (fleet_direct_config.MAX_ADVERTISE_ADDRS).
MAX_ADDRESSES = 8
#: BYTES, not characters. The unit is stated because writer, verifier, schema
#: and any cache must measure identically; a char limit and a byte limit
#: disagree on any non-ASCII host and the signature would then cover bytes one
#: side considered over-length.
MAX_ADDRESS_BYTES = 256
MAX_RELAY_BASE_BYTES = 256
#: Final canonical UTF-8 bytes of the whole row INCLUDING ``sig``. Asserted by a
#: test that builds the largest valid descriptor and proves it fits; if it ever
#: does not, a bound changes explicitly rather than anything being truncated.
MAX_ROW_BYTES = 4096

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX128_RE = re.compile(r"^[0-9a-f]{128}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_ROW_FIELDS = frozenset({"v", "machine_pub", "addresses", "relay", "updated_at", "sig"})
_RELAY_FIELDS = frozenset({"relay_base", "org_uuid", "persona_pub", "serving_machine_pub"})


def row_bytes(payload: Any) -> int:
    """Final canonical UTF-8 byte length of *payload*.

    THE one definition. Writer, verifier and schema all measure through this
    function so a bound can never mean different things in two places.
    """
    from tools.network.idkit.canonical import canonical_json

    return len(canonical_json(payload))


@publication_band(min="raw", max="raw")
@home("personal")
@keyed_per_entity(key_strategy="machine_pub")
class PersonalFleetReachabilityV1(SettingSchema):
    """One personal-fleet machine's direct addresses and optional relay slot."""

    set_id = PERSONAL_FLEET_REACHABILITY_SET_ID
    schema_revision = PERSONAL_FLEET_REACHABILITY_REVISION

    _field_metadata: dict[str, dict] = {
        "v": {"type": "integer", "required": True, "description": "Row version (1)"},
        "machine_pub": {
            "type": "string", "required": True,
            "description": "Durable roster machine public key (64 hex); equals the row key",
        },
        "addresses": {
            "type": "array", "required": True, "items": {"type": "string"},
            "description": (
                "Direct ws:// or wss:// sync listener URLs. ORDER IS SIGNED "
                "CANDIDATE PRIORITY, best first, and is never sorted"
            ),
        },
        "relay": {
            "type": "object", "required": False,
            "description": (
                "null, or the serving slot locator {relay_base, org_uuid, "
                "persona_pub, serving_machine_pub}. Routing only: it confers "
                "no membership and no scope"
            ),
        },
        "updated_at": {
            "type": "integer", "required": True,
            "description": (
                "Unix seconds when the semantic tuple last changed. Convergence "
                "metadata only — NOT anti-rollback"
            ),
        },
        "sig": {
            "type": "string", "required": True,
            "description": "Machine-key signature (128 hex) over the domain-separated row",
        },
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        name = cls.__name__
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{name}: payload must be a dict, got {type(payload).__name__}"
            )
        # EXACT field set. An unknown field is refused rather than ignored: it
        # would be covered by the signature on one side and dropped on the
        # other, so two honest parties would disagree about what was signed.
        unknown = sorted(set(payload) - _ROW_FIELDS)
        missing = sorted(_ROW_FIELDS - set(payload) - {"relay"})
        if unknown or missing:
            raise SchemaValidationError(
                f"{name}: field set must be exactly {sorted(_ROW_FIELDS)}"
                + (f" (unknown {unknown})" if unknown else "")
                + (f" (missing {missing})" if missing else "")
            )
        if payload.get("v") != ROW_VERSION:
            raise SchemaValidationError(f"{name}: 'v' must be {ROW_VERSION}")
        machine_pub = payload.get("machine_pub")
        if not isinstance(machine_pub, str) or not _HEX64_RE.match(machine_pub):
            raise SchemaValidationError(
                f"{name}: 'machine_pub' must be 64 lowercase hex chars"
            )

        addresses = payload.get("addresses")
        if not isinstance(addresses, list) or len(addresses) > MAX_ADDRESSES:
            raise SchemaValidationError(
                f"{name}: 'addresses' must be a list of at most {MAX_ADDRESSES}"
            )
        for address in addresses:
            if not isinstance(address, str) or not address:
                raise SchemaValidationError(f"{name}: every address must be a non-empty string")
            if len(address.encode("utf-8")) > MAX_ADDRESS_BYTES:
                raise SchemaValidationError(
                    f"{name}: every address must be at most {MAX_ADDRESS_BYTES} UTF-8 bytes"
                )
            if not (address.startswith("ws://") or address.startswith("wss://")):
                raise SchemaValidationError(f"{name}: every address must be a ws:// or wss:// URL")
        # Uniqueness is required of the RECEIVED row, not imposed on it. A
        # verifier that deduplicated untrusted input would be verifying a body
        # the signer never produced.
        if len(set(addresses)) != len(addresses):
            raise SchemaValidationError(f"{name}: 'addresses' must already be unique")

        relay = payload.get("relay")
        if relay is not None:
            if not isinstance(relay, dict) or set(relay) != _RELAY_FIELDS:
                raise SchemaValidationError(
                    f"{name}: 'relay' must be null or exactly {sorted(_RELAY_FIELDS)}"
                )
            relay_base = relay.get("relay_base")
            if (not isinstance(relay_base, str) or not relay_base
                    or len(relay_base.encode("utf-8")) > MAX_RELAY_BASE_BYTES):
                raise SchemaValidationError(
                    f"{name}: 'relay.relay_base' must be at most "
                    f"{MAX_RELAY_BASE_BYTES} UTF-8 bytes"
                )
            if not _UUID_RE.match(str(relay.get("org_uuid"))):
                raise SchemaValidationError(f"{name}: 'relay.org_uuid' must be a canonical uuid")
            for field in ("persona_pub", "serving_machine_pub"):
                value = relay.get(field)
                if not isinstance(value, str) or not _HEX64_RE.match(value):
                    raise SchemaValidationError(
                        f"{name}: 'relay.{field}' must be 64 lowercase hex chars"
                    )

        updated_at = payload.get("updated_at")
        if isinstance(updated_at, bool) or not isinstance(updated_at, int) or updated_at < 0:
            raise SchemaValidationError(
                f"{name}: 'updated_at' must be a non-negative integer"
            )
        sig = payload.get("sig")
        if not isinstance(sig, str) or not _HEX128_RE.match(sig):
            raise SchemaValidationError(f"{name}: 'sig' must be 128 lowercase hex chars")

        if row_bytes(payload) > MAX_ROW_BYTES:
            raise SchemaValidationError(
                f"{name}: canonical row exceeds {MAX_ROW_BYTES} UTF-8 bytes"
            )

    @classmethod
    def validate_member_key(cls, key: str) -> None:
        if not _HEX64_RE.match(key or ""):
            raise SchemaValidationError(
                f"{cls.__name__}: keys are machine public keys (64 lowercase hex), got {key!r}"
            )
