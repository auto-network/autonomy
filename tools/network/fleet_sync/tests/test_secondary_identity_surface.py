"""Every UNIQUE constraint a replication key does not cover is declared.

A replicated row is identified on the wire by its policy key. If the table
also has a UNIQUE constraint on some OTHER column, two machines can produce
rows that are distinct under the key and identical under the constraint. The
arriving INSERT then fails, and before 2026-09-08 that aborted the whole
batch: the watermark never advanced and the identical batch replayed every
24 seconds. One such row on `entities.canonical_name` blocked 2.8M rows of
real content for a day.

The batch no longer aborts -- the row is skipped and quarantined as
`secondary_identity_conflict`. This test is the other half: nobody should
have to rediscover the surface by watching a scope stall. Every uncovered
constraint is listed here with how it is handled, and a new one fails CI.
"""

from pathlib import Path

from tools.graph.db import GraphDB
from tools.network.fleet_sync.policies import PolicyKind, TABLE_POLICIES


#: table -> {constraint columns: why this cannot stall the fleet}.
#: Add an entry ONLY with a real reason. "Probably fine" is not one.
ACKNOWLEDGED: dict[str, dict[tuple[str, ...], str]] = {
    "sources": {
        ("file_path",): (
            "materialize forces file_path to NULL on every arriving source "
            "(it is machine-local provenance), and SQLite's UNIQUE does not "
            "constrain NULLs, so arriving rows cannot collide here."
        ),
    },
    "edges": {
        ("id",): (
            "id is excluded from the policy and re-derived deterministically "
            "by _edge_id from the replication key, so both machines compute "
            "the same id for the same logical edge."
        ),
    },
    "settings": {
        ("id",): (
            "id is a local row identifier; the settings branch of _upsert "
            "deletes the addressed slot before inserting, so the incoming "
            "row replaces rather than collides."
        ),
        ("set_id", "schema_revision", "key", "publication_state",
         "terminal_persona"): (
            "the one-slot-per-signer rule; the settings branch of _upsert "
            "targets exactly that slot and clears it first."
        ),
    },
    "note_versions": {
        ("source_id", "version"): (
            "note_versions is SPECIAL: _apply_note_versions renumbers the "
            "machine-local `version` column, which is why content identity "
            "(source_id, created_at, content_hash) is the replication key."
        ),
    },
    "thoughts": {
        ("source_id", "message_id"): (
            "UNCOVERED. Two machines that mint their own thought rows for the "
            "same replicated source and message collide here. Contained, not "
            "prevented: the row is skipped and quarantined as "
            "secondary_identity_conflict, and fleet_doctor reports the "
            "backlog by reason. Making the row id deterministic would remove "
            "the class."
        ),
    },
    "derivations": {
        ("source_id", "message_id"): (
            "UNCOVERED, identical to thoughts above."
        ),
    },
    "attachments": {
        ("file_path",): (
            "UNCOVERED. file_path is content-addressed, so two attachment ids "
            "holding identical bytes resolve to one path. Contained the same "
            "way: skipped and quarantined, never fatal."
        ),
    },
}


def _uncovered_constraints(conn) -> dict[str, set[tuple[str, ...]]]:
    found: dict[str, set[tuple[str, ...]]] = {}
    for table, policy in sorted(TABLE_POLICIES.items()):
        if policy.kind in {PolicyKind.LOCAL, PolicyKind.DERIVED}:
            continue
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone():
            continue
        for row in conn.execute(f'PRAGMA index_list("{table}")'):
            name, is_unique = str(row[1]), int(row[2])
            if not is_unique:
                continue
            columns = [c[2] for c in conn.execute(f'PRAGMA index_info("{name}")')]
            if any(c is None for c in columns):
                continue  # expression index: no column identity to compare
            if set(columns) <= set(policy.key):
                continue
            found.setdefault(table, set()).add(tuple(columns))
    return found


def test_every_uncovered_unique_constraint_is_declared(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        from tools.vault.store import _SCHEMA as VAULT_SCHEMA
        db.conn.executescript(VAULT_SCHEMA)
        found = _uncovered_constraints(db.conn)
    finally:
        db.close()

    undeclared = sorted(
        f"{table}{list(columns)}"
        for table, sets in found.items()
        for columns in sets
        if columns not in ACKNOWLEDGED.get(table, {})
    )
    assert not undeclared, (
        "these UNIQUE constraints are not covered by their table's "
        "replication key, so two machines can produce rows that are distinct "
        "under the key and identical under the constraint: "
        f"{undeclared}. Either widen the policy key to the constraint, derive "
        "the colliding column deterministically, or add an entry to "
        "ACKNOWLEDGED in this file saying why it cannot stall a scope."
    )
    # And nothing stale: an entry whose constraint no longer exists is a
    # claim about the schema that is no longer true.
    stale = sorted(
        f"{table}{list(columns)}"
        for table, entries in ACKNOWLEDGED.items()
        for columns in entries
        if columns not in found.get(table, set())
    )
    assert not stale, f"ACKNOWLEDGED describes constraints that are gone: {stale}"
