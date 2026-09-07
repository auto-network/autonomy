"""``autonomy.org.fleet-reachability#1`` — where an organization's members'
machines can be dialled for org-scope sync, kept by those machines
themselves (auto-mldvv, finding graph://26daff34-1ae).

Operator direction 2026-09-07: the directory of an organization's machines
is not a central service; it is state the organization's own machines keep
and replicate. One row per machine, keyed by the machine public key,
written by that machine alone and ONLY when its address set changes --
never as a heartbeat. Liveness is learned by contact (fleet_sync_peer_state),
so a dead address costs one bounded connect attempt and no row rewrites in
response to another row: no feedback loop.

Rows are UNSIGNED settings rows (as autonomy.org.ledger-event#1 is): the
authority evidence is inside the payload -- the member persona's
certificate to the machine key and the machine key's signature over the
row -- verified by every reader (tools/network/fleet_org_reachability),
which also drops rows whose persona is outside the adopted member set.
Rows are hints, never admission: the org hello (auto-coea3) decides.
"""

from __future__ import annotations

import re
from typing import Any

from .registry import (
    SettingSchema,
    SchemaValidationError,
    home,
    publication_band,
)

ORG_FLEET_REACHABILITY_SET_ID = "autonomy.org.fleet-reachability"
ORG_FLEET_REACHABILITY_REVISION = 1
ROW_VERSION = 1
#: fleet_direct_config.MAX_ADVERTISE_ADDRS — the personal announce's bound.
MAX_ADDRESSES = 8
MAX_ADDRESS_CHARS = 256

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


@publication_band(min="raw", max="published")
@home("organization")
class OrgFleetReachabilityV1(SettingSchema):
    """One member machine's direct sync addresses, self-certified."""

    set_id = ORG_FLEET_REACHABILITY_SET_ID
    schema_revision = ORG_FLEET_REACHABILITY_REVISION

    _field_metadata: dict[str, dict] = {
        "v": {"type": "integer", "required": True, "description": "Row version (1)"},
        "machine_pub": {
            "type": "string", "required": True,
            "description": "The machine's public key (64 hex); equals the row key",
        },
        "persona_pub": {
            "type": "string", "required": True,
            "description": "The member persona this machine belongs to (64 hex)",
        },
        "persona_cert": {
            "type": "object", "required": True,
            "description": (
                "idkit certificate issued by the persona to the machine key, "
                "scope fleet:sync, org = the genesis id"
            ),
        },
        "addresses": {
            "type": "array", "required": True, "items": {"type": "string"},
            "description": "Direct ws:// or wss:// sync listener URLs, best first",
        },
        "updated_at": {
            "type": "integer", "required": True,
            "description": "Unix seconds when the address set last changed",
        },
        "sig": {
            "type": "string", "required": True,
            "description": "Machine-key signature over the domain-separated row",
        },
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, got {type(payload).__name__}"
            )
        if payload.get("v") != ROW_VERSION:
            raise SchemaValidationError(f"{cls.__name__}: 'v' must be {ROW_VERSION}")
        for field in ("machine_pub", "persona_pub"):
            value = payload.get(field)
            if not isinstance(value, str) or not _HEX64_RE.match(value):
                raise SchemaValidationError(
                    f"{cls.__name__}: {field!r} must be 64 lowercase hex chars"
                )
        if not isinstance(payload.get("persona_cert"), dict):
            raise SchemaValidationError(f"{cls.__name__}: 'persona_cert' must be an object")
        addresses = payload.get("addresses")
        if not isinstance(addresses, list) or len(addresses) > MAX_ADDRESSES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'addresses' must be a list of at most {MAX_ADDRESSES}"
            )
        for address in addresses:
            if (
                not isinstance(address, str) or not address
                or len(address) > MAX_ADDRESS_CHARS
                or not (address.startswith("ws://") or address.startswith("wss://"))
            ):
                raise SchemaValidationError(
                    f"{cls.__name__}: every address must be a ws:// or wss:// URL"
                )
        updated_at = payload.get("updated_at")
        if isinstance(updated_at, bool) or not isinstance(updated_at, int) or updated_at < 0:
            raise SchemaValidationError(
                f"{cls.__name__}: 'updated_at' must be a non-negative integer"
            )
        if not isinstance(payload.get("sig"), str) or not payload["sig"]:
            raise SchemaValidationError(f"{cls.__name__}: 'sig' must be a non-empty string")

    @classmethod
    def validate_member_key(cls, key: str) -> None:
        if not _HEX64_RE.match(key or ""):
            raise SchemaValidationError(
                f"{cls.__name__}: keys are machine public keys (64 lowercase hex), got {key!r}"
            )
