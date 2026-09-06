"""``autonomy.org.ledger-event#1`` — a ledger event as a replicated Setting row.

Operator ruling 2026-09-06 (bead auto-dqemk): standardize on Settings as the
table-synchronization method. The org authority ledger's events become rows
of an org-homed append-only set so the existing settings replication carries
them; each receiving node feeds an arriving row's ``wire`` through
``LedgerStore.append_wire``, which re-verifies the content hash and the
author signature and maintains that store's own ``ledger_parents`` /
``ledger_heads`` indexes. The row is the transport, never the store: the
ledger tables stay LOCAL in fleet-sync policy and are rebuilt per node.

Rows are deliberately UNSIGNED settings rows: the event's own ``author_key``
and ``sig`` inside ``wire`` are the authority evidence, verified by the
ledger parser on every ingest. Requiring the settings envelope here would
add a second signature over the same bytes and a bootstrap circle (the
envelope verifies against the org genesis, which is itself an event).
"""

from __future__ import annotations

import re
from typing import Any

from .registry import (
    SettingSchema,
    SchemaValidationError,
    append_only_log,
    home,
    publication_band,
)

ORG_LEDGER_EVENT_SET_ID = "autonomy.org.ledger-event"
ORG_LEDGER_EVENT_REVISION = 1

#: tools/network/ledger/events.py MAX_EVENT_BYTES — the parser refuses
#: anything larger, so a row over this bound could never absorb.
MAX_WIRE_CHARS = 16_384

_EVENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")


@publication_band(min="raw", max="published")
@home("organization")
@append_only_log(key="event_id_sha256")
class OrgLedgerEventV1(SettingSchema):
    """One signed authority event, keyed by its content hash.

    The key IS ``sha256(wire)`` (the ledger's event id), which makes
    concurrent publication of the same event on two nodes converge to
    duplicate-keyed rows a reader deduplicates by key — never a conflict.
    """

    set_id = ORG_LEDGER_EVENT_SET_ID
    schema_revision = ORG_LEDGER_EVENT_REVISION

    _field_metadata: dict[str, dict] = {
        "wire": {
            "type": "string",
            "required": True,
            "max_length": MAX_WIRE_CHARS,
            "description": (
                "The event's canonical JSON wire, verbatim — the bytes whose "
                "sha256 is the row key and whose embedded sig the ledger "
                "parser verifies on ingest"
            ),
        },
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        wire = payload.get("wire")
        if not isinstance(wire, str) or not wire:
            raise SchemaValidationError(
                f"{cls.__name__}: 'wire' must be a non-empty string"
            )

    @classmethod
    def validate_member_key(cls, key: str) -> None:
        if not _EVENT_ID_RE.match(key or ""):
            raise SchemaValidationError(
                f"{cls.__name__}: keys are ledger event ids "
                f"(64 lowercase hex chars of sha256(wire)), got {key!r}"
            )
