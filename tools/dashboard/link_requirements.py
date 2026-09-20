"""R, the persona requirement of a link (auto-xs9hz, graph://d9153c5a-76e O-C).

A link serves rows. Every row was authored by one origin machine, recorded
in the org store's catalog with that origin and the origin's own timestamp.
The publisher maps each origin to the persona it belongs to (its own
personal roster for its own machines; the persona write floor record listing the
machine for every other persona) and keeps the newest timestamp per
persona. The registry stores that map on the link row and the relay dials
only members whose advertised frontiers cover every entry.

An author machine that no persona write floor record lists yet cannot be
attributed, and the publish refuses with that machine named rather than
guessing (expected only for a persona whose first write floor has not propagated).
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Mapping

from tools.network.fleet_sync import write_floors
from tools.network.fleet_sync.codec import encode_value
from tools.network.fleet_sync.policies import TABLE_POLICIES
from tools.network.fleet_sync.snapshot import _logical_address


class LinkRequirementError(RuntimeError):
    """A served row's author machine cannot be attributed to a persona."""


def row_origins(conn: sqlite3.Connection, addresses: Iterable[bytes]) -> dict[str, int]:
    """``{origin: newest timestamp_ns}`` over the catalog rows at *addresses*.
    A row with no catalog entry (never replicated, e.g. written before the
    store was activated) is attributed to nobody and skipped."""
    out: dict[str, int] = {}
    for address in addresses:
        row = conn.execute(
            "SELECT o.incarnation, c.timestamp_ns FROM fleet_sync_catalog c "
            "JOIN fleet_sync_transactions t ON t.id=c.transaction_ref "
            "JOIN fleet_sync_origins o ON o.id=t.origin_id WHERE c.address=?",
            (address,),
        ).fetchone()
        if row is None:
            continue
        origin, stamp = str(row[0]), int(row[1])
        if stamp > out.get(origin, -1):
            out[origin] = stamp
    return out


def note_addresses(conn: sqlite3.Connection, source_id: str) -> list[bytes]:
    """The catalog addresses a note link serves: the source row, its
    thoughts, and the attachments filed under it."""
    addresses = [encode_value(["sources", [source_id]])]
    for (thought_id,) in conn.execute(
        "SELECT id FROM thoughts WHERE source_id=?", (source_id,)
    ):
        addresses.append(encode_value(["thoughts", [str(thought_id)]]))
    for (attachment_id,) in conn.execute(
        "SELECT id FROM attachments WHERE source_id=?", (source_id,)
    ):
        addresses.append(encode_value(["attachments", [str(attachment_id)]]))
    return addresses


def settings_addresses(conn: sqlite3.Connection, set_id: str, key: str) -> list[bytes]:
    """The catalog addresses of every settings row at (*set_id*, *key*),
    for the link's own grant row."""
    policy = TABLE_POLICIES["settings"]
    return [
        encode_value(["settings", list(_logical_address(policy, dict(row)))])
        for row in conn.execute(
            "SELECT * FROM settings WHERE set_id=? AND key=?", (set_id, key)
        )
    ]


def personas_of_origins(
    conn: sqlite3.Connection, origins: Mapping[str, int], *,
    own_machines: Iterable[str], own_persona: str,
) -> dict[str, int]:
    """Fold ``{origin: ts}`` into ``{persona: newest ts}``."""
    own = set(own_machines)
    listed: dict[str, str] = {}
    for persona, record in write_floors.persona_write_floors(conn).items():
        for machine in (record.get("machines") or {}):
            listed[str(machine)] = persona
    out: dict[str, int] = {}
    for origin, stamp in origins.items():
        persona = own_persona if origin in own else listed.get(origin)
        if persona is None:
            raise LinkRequirementError(
                f"author machine {origin[:12]} is not yet attributed to a "
                "persona (no persona write floor lists it); publish again after one "
                "sync round"
            )
        if stamp > out.get(persona, -1):
            out[persona] = stamp
    return out


def link_requirements(
    conn: sqlite3.Connection, *, source_id: str, grant_set_id: str, grant_key: str,
    own_machines: Iterable[str], own_persona: str,
    key_set_id: str | None = None,
) -> dict[str, int]:
    """R for a note link: its rows, its grant row and, when *key_set_id* is
    given, its channel key row under the same key, folded to personas. Built
    after those rows commit and sent in the one create-link (O-C)."""
    addresses = note_addresses(conn, source_id) + settings_addresses(conn, grant_set_id, grant_key)
    if key_set_id is not None:
        addresses += settings_addresses(conn, key_set_id, grant_key)
    return personas_of_origins(
        conn, row_origins(conn, addresses),
        own_machines=own_machines, own_persona=own_persona,
    )
