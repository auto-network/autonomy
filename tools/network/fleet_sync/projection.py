"""Audience projections over the replicated surface (bead auto-cshno).

Two projections decide *which* rows a serve emits, over the one policy
inventory every peer already agrees on (the ``compatibility_digest`` never
changes; a projection is a filter, not a new surface).

- :attr:`Projection.FULL` is the whole replicated surface every fleet and org
  peer receives. Every predicate here admits every row, so a FULL serve is
  byte-identical to the engine before this module existed.

- :attr:`Projection.PUBLIC` is the surface an ``org:follow`` admission
  receives (design of record graph://5f2f5a49-00d §10.2): an organization's
  **published and canonical** ``sources``, the ``thoughts``/``derivations``/
  ``tags``/``attachments`` that name one of those sources, and its published,
  non-deprecated ``settings``. Every other table is excluded -- never emitted
  and never tombstoned, because a follower never held it.

The PUBLIC source predicate reuses
:data:`tools.graph.cross_org.PEER_VISIBLE_STATES`, the same publication ladder
the on-disk cross-org read filter enforces, so the wire projection and the
read filter can never drift apart.

A note on ``tags``. The record names four satellite tables filtered by
``source_id``; three of them (``thoughts``, ``derivations``, ``attachments``)
carry a ``source_id`` column that references a source. The ``tags`` table is a
global name->description registry keyed by ``name`` with no ``source_id``
column (a source's own tags live in ``sources.metadata``, and travel with the
source row). It therefore names no source and is never admitted into the
public surface -- which is exactly the predicate "admitted when their
``source_id`` names an admitted source", read literally, and is safe: no
private row leaks. It is kept in :data:`SATELLITE_TABLES` so the tuple matches
the record, and the SQL / enumeration helpers below never reference a
``source_id`` column it does not have.
"""

from __future__ import annotations

import enum
from typing import Mapping

from tools.graph.cross_org import PEER_VISIBLE_STATES


class Projection(enum.Enum):
    """The audience a serve is projecting for."""

    FULL = "full"
    PUBLIC = "public"


#: The dependent tables whose rows belong to the public surface only through a
#: source they name. Fixed by the record §10.2 as exactly these four;
#: ``note_comments`` and ``captures`` are excluded by FR2 and never appear.
SATELLITE_TABLES = ("thoughts", "derivations", "tags", "attachments")

#: The satellite tables that actually carry a ``source_id`` column, and so can
#: name a source. ``tags`` (see the module docstring) does not.
SOURCE_LINKED_SATELLITE_TABLES = ("thoughts", "derivations", "attachments")

#: Every table the PUBLIC projection can emit. Anything not here is excluded.
PROJECTED_TABLES = ("sources", *SATELLITE_TABLES, "settings")

#: ``'published', 'canonical'`` as a SQL value list, built from
#: PEER_VISIBLE_STATES so the states are stated in exactly one place.
PUBLIC_STATES_SQL = ", ".join(f"'{state}'" for state in PEER_VISIBLE_STATES)


def source_row_is_public(row: Mapping[str, object]) -> bool:
    """A ``sources`` row is admitted iff its state is peer-visible."""
    return row.get("publication_state") in PEER_VISIBLE_STATES


#: The settings sets a follower may receive. Publication state alone is not
#: the public surface: members replicate the org ledger, member profiles and
#: fleet reachability at ``published`` so that MEMBERS get them, and the
#: first real follow (2026-09-25, Boatlore <- Autonomy) carried all of it —
#: 21 ledger events that built the members and roles on the follower, two
#: machines' serving keys and addresses, the coordinator action registry.
#: A set crosses a follow only when named here, whatever its rows' state.
#: The org identity row (name, byline, color) is what lets the follower show
#: the org it follows; the primers and capability contracts are what the
#: bootstrap package resolves from the public surface (design of record
#: graph://5f2f5a49-00d FR2, D5). Add a set here deliberately, with the
#: operator's ruling, never by promoting its rows.
FOLLOW_VISIBLE_SET_IDS: frozenset[str] = frozenset({
    "autonomy.org",
    "autonomy.org.primer",
    "autonomy.org.capability.primer",
    "autonomy.capability.contract",
})

#: ``FOLLOW_VISIBLE_SET_IDS`` as a SQL value list.
FOLLOW_VISIBLE_SET_IDS_SQL = ", ".join(
    f"'{set_id}'" for set_id in sorted(FOLLOW_VISIBLE_SET_IDS)
)


def settings_row_is_public(row: Mapping[str, object]) -> bool:
    """A ``settings`` row is admitted iff its set is follower-visible, it is
    peer-visible and it is not deprecated."""
    return (
        row.get("set_id") in FOLLOW_VISIBLE_SET_IDS
        and row.get("publication_state") in PEER_VISIBLE_STATES
        and not row.get("deprecated")
    )


def public_predicate_sql(table: str) -> str | None:
    """The SQL admittance expression for *table* under PUBLIC.

    Returns a boolean SQL fragment (no leading ``WHERE``/``AND``) that a base
    walk or sweep can conjoin into its per-table query, or ``None`` when the
    table is excluded from the public surface entirely (the caller skips it).
    Never references a column a table does not have, so it is safe to splice.
    """
    if table == "sources":
        return f"publication_state IN ({PUBLIC_STATES_SQL})"
    if table == "settings":
        return (
            f"set_id IN ({FOLLOW_VISIBLE_SET_IDS_SQL}) AND "
            f"publication_state IN ({PUBLIC_STATES_SQL}) AND deprecated = 0"
        )
    if table in SOURCE_LINKED_SATELLITE_TABLES:
        return (
            f"source_id IN (SELECT id FROM sources "
            f"WHERE publication_state IN ({PUBLIC_STATES_SQL}))"
        )
    if table in SATELLITE_TABLES:
        # A satellite table with no source_id column (``tags``) names no
        # source, so nothing in it is ever admitted.
        return "0"
    return None
