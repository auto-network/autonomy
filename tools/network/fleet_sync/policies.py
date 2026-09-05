"""Complete logical-table inventory for personal graph replication.

The inventory is executable documentation: opening a current GraphDB and
calling :func:`audit_schema` fails if a durable table is neither replicated nor
explicitly classified as derived/local.  That keeps a schema migration from
quietly creating graph state the checkpoint codec never carries.

Policies describe logical identity, not raw SQLite layout.  Mutation time and
tombstones live in the replication envelope; many existing tables do not keep
a complete mutation history themselves, so a snapshot cannot reconstruct that
history after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import sqlite3
from typing import Final


class PolicyKind(str, Enum):
    """How logical rows from a table converge."""

    UNION = "union"
    LWW = "timestamp-lww"
    SPECIAL = "special"
    EXTERNAL_BLOB = "external-content-addressed-blob"
    IMMUTABLE = "immutable-content-addressed"
    IMMUTABLE_PRUNABLE = "immutable-content-addressed-local-body-prune"
    LOCAL = "local-only"
    DERIVED = "derived-rebuilt"


@dataclass(frozen=True)
class TablePolicy:
    """Stable logical projection of one SQLite table."""

    table: str
    kind: PolicyKind
    key: tuple[str, ...]
    json_columns: tuple[str, ...] = ()
    excluded_columns: tuple[str, ...] = ()
    timestamp_columns: tuple[str, ...] = ("updated_at", "created_at")
    note: str = ""


# Full durable schema from tools/graph/schema.sql plus GraphDB migrations.
# A logical mutation envelope supplies timestamp/order independently of these
# columns.  `updated_at` is retained as graph content where present, but is not
# trusted as the replication envelope's receipt/order clock.
TABLE_POLICIES: Final[dict[str, TablePolicy]] = {
    "sources": TablePolicy(
        "sources", PolicyKind.LWW, ("id",), ("metadata",), ("file_path",),
        ("last_activity_at", "ingested_at", "created_at"),
        "file_path is machine-local provenance and is not a remotely openable path",
    ),
    "thoughts": TablePolicy(
        "thoughts", PolicyKind.LWW, ("id",), ("tags", "metadata")
    ),
    "derivations": TablePolicy(
        "derivations", PolicyKind.LWW, ("id",), ("metadata",)
    ),
    "entities": TablePolicy(
        "entities", PolicyKind.LWW, ("id",), ("metadata",),
    ),
    "claims": TablePolicy(
        "claims", PolicyKind.LWW, ("id",), ("metadata",)
    ),
    "edges": TablePolicy(
        "edges", PolicyKind.LWW,
        ("source_id", "target_id", "relation"),
        ("metadata",), ("id",),
        ("created_at",),
        "matches the table's declared uniqueness rather than a random row id",
    ),
    "entity_mentions": TablePolicy(
        "entity_mentions", PolicyKind.LWW, ("entity_id", "content_id")
    ),
    "nodes": TablePolicy(
        "nodes", PolicyKind.LWW, ("id",), ("metadata",)
    ),
    "node_refs": TablePolicy(
        "node_refs", PolicyKind.LWW, ("node_id", "ref_id"), ("metadata",)
    ),
    "note_comments": TablePolicy(
        "note_comments", PolicyKind.LWW, ("id",)
    ),
    "note_versions": TablePolicy(
        "note_versions", PolicyKind.SPECIAL,
        ("source_id", "created_at", "content_hash"),
        excluded_columns=("id", "version"),
        timestamp_columns=("created_at",),
        note=(
            "content identity survives concurrent machine-local autoincrement/version "
            "collisions; display version is recomputed after merge"
        ),
    ),
    "attachments": TablePolicy(
        "attachments", PolicyKind.EXTERNAL_BLOB, ("id",), ("metadata",),
        ("file_path",),
        ("created_at",),
        "metadata replicates here; bytes transfer separately by content hash",
    ),
    "note_reads": TablePolicy(
        "note_reads", PolicyKind.UNION, ("source_id", "actor", "ts"),
        timestamp_columns=("ts",),
    ),
    "tags": TablePolicy(
        "tags", PolicyKind.LWW, ("name",)
    ),
    "threads": TablePolicy(
        "threads", PolicyKind.LWW, ("id",), ("metadata",)
    ),
    "captures": TablePolicy(
        "captures", PolicyKind.LWW, ("id",), ("metadata",)
    ),
    "settings": TablePolicy(
        "settings", PolicyKind.SPECIAL,
        ("set_id", "schema_revision", "key", "publication_state", "row_role"),
        ("payload", "witness"),
        timestamp_columns=("signed_at", "updated_at", "created_at"),
        note=(
            "base rows use the natural address; override/exclusion roles retain their "
            "target identity"
        ),
    ),
    "policy_classes": TablePolicy(
        "policy_classes", PolicyKind.LWW, ("class_id",),
        timestamp_columns=(),
        note="personal vault policy-class records synchronize across the fleet",
    ),
    "vault_factors": TablePolicy(
        "vault_factors", PolicyKind.LWW, ("factor_id",),
        timestamp_columns=(),
        note="personal vault factor records synchronize across the fleet",
    ),
    "root_anchors": TablePolicy(
        "root_anchors", PolicyKind.LWW, ("anchor_id",),
        timestamp_columns=(),
        note="personal root-anchor records synchronize across the fleet",
    ),
    "vault_secrets": TablePolicy(
        "vault_secrets", PolicyKind.LWW, ("setting_name",),
        timestamp_columns=(),
        note="sealed personal vault references synchronize across the fleet",
    ),
    "vault_content_bodies": TablePolicy(
        "vault_content_bodies", PolicyKind.IMMUTABLE,
        ("ciphertext_hash",), timestamp_columns=(),
        note=(
            "AEAD ciphertext is carried as canonical BLOB bytes; identical replay "
            "is a no-op and any same-hash byte difference fails closed"
        ),
    ),
    "vault_content_objects": TablePolicy(
        "vault_content_objects", PolicyKind.IMMUTABLE,
        ("object_id", "revision_id"), timestamp_columns=(),
        note=(
            "immutable encrypted-object header; header_json is exact BLOB bytes, "
            "not graph JSON text"
        ),
    ),
    "vault_state_object_counts": TablePolicy(
        "vault_state_object_counts", PolicyKind.DERIVED, (), timestamp_columns=(),
        note="rebuilt as COUNT(*) grouped by storage_state_id after materialization",
    ),
    "keycontrol_state": TablePolicy(
        "keycontrol_state", PolicyKind.IMMUTABLE, ("state_id",),
        timestamp_columns=(),
        note="signed storage-state descriptor wire bytes",
    ),
    "keycontrol_grant": TablePolicy(
        "keycontrol_grant", PolicyKind.IMMUTABLE, ("grant_id",),
        timestamp_columns=(),
        note=(
            "signed capability-grant wire bytes; immutable content-addressed "
            "record required to open a newly synchronized generation"
        ),
    ),
    "keycontrol_credential": TablePolicy(
        "keycontrol_credential", PolicyKind.IMMUTABLE_PRUNABLE,
        ("kem_key_id",), timestamp_columns=(),
        note=(
            "immutable signed credential; wire may be pruned to NULL locally, "
            "but a non-NULL peer copy always restores it"
        ),
    ),
    "keycontrol_bridge": TablePolicy(
        "keycontrol_bridge", PolicyKind.IMMUTABLE_PRUNABLE,
        ("bridge_id",), timestamp_columns=(),
        note=(
            "immutable signed parent bridge; wire may be pruned to NULL locally, "
            "but a non-NULL peer copy always restores it"
        ),
    ),
    "keycontrol_meta": TablePolicy(
        "keycontrol_meta", PolicyKind.LOCAL, (), timestamp_columns=(),
        note="this machine's key-control schema/progress metadata",
    ),
    "keycontrol_pending": TablePolicy(
        "keycontrol_pending", PolicyKind.LOCAL, (), timestamp_columns=(),
        note="this machine's dependency wait queue",
    ),
    "keycontrol_pending_usage": TablePolicy(
        "keycontrol_pending_usage", PolicyKind.LOCAL, (), timestamp_columns=(),
        note="locally derived key-control pending-queue counters",
    ),
    "orgs": TablePolicy(
        "orgs", PolicyKind.LOCAL, (), timestamp_columns=(),
        note="physical store bootstrap identity, not personal graph content",
    ),
}


DERIVED_TABLE_PREFIXES: Final[tuple[str, ...]] = (
    "thoughts_fts",
    "derivations_fts",
    "captures_fts",
    "sources_fts",
)

LOCAL_SYNC_TABLES: Final[frozenset[str]] = frozenset({
    "fleet_sync_catalog",
    "fleet_sync_journal",
    "fleet_sync_origins",
    "fleet_sync_peer_state",
    "fleet_sync_quarantine",
    "fleet_sync_state",
    "fleet_sync_transactions",
})

#: Ledger state is node-local, keyed by genesis id — fleet authority reaches
#: a member via the root-signed roster delivery, not by replicating raw
#: ledger rows (whose cross-machine merge is undecided).
LEDGER_TABLES: Final[frozenset[str]] = frozenset({
    "ledger_events",
    "ledger_heads",
    "ledger_meta",
    "ledger_parents",
    "ledger_pending_claims",
    "ledger_projections",
})


#: Personal-scope tables that are node-local by construction: each machine
#: derives them itself and they are NEVER replicated, so they carry no wire
#: policy and MUST stay out of the replication surface / compatibility digest
#: (unlike a LOCAL entry in TABLE_POLICIES, which the digest still folds in).
PERSONAL_LOCAL_TABLES: Final[frozenset[str]] = frozenset({
    # The audited delegate recipient's X25519 PUBLIC half: deterministic from
    # the operator's root seed and derived per-machine at unlock (auto-ie6yr),
    # so every member independently reaches the identical value and nothing
    # needs to go on the wire. INTERIM home: whether this pubkey should instead
    # live as a Setting (the vault's "Setting resolution IS the release
    # mechanism" model, graph://83c92d72-0ed) is still open on auto-ie6yr. It
    # arrived as a bespoke table (commit e87bd7ae) with no policy at all, which
    # surfaced as an unclassified-table error on the operator's unlock screen.
    "delegate_recipients",
})


def classify_table(name: str) -> PolicyKind | None:
    """Return the explicit classification for *name*, or ``None``."""

    policy = TABLE_POLICIES.get(name)
    if policy is not None:
        return policy.kind
    if name.startswith(DERIVED_TABLE_PREFIXES):
        return PolicyKind.DERIVED
    if name in LOCAL_SYNC_TABLES:
        return PolicyKind.LOCAL
    if name in LEDGER_TABLES:
        return PolicyKind.LOCAL
    if name in PERSONAL_LOCAL_TABLES:
        return PolicyKind.LOCAL
    return None


_COMPAT_DIGEST_DOMAIN = b"autonomy.network.fleet-sync.compat-digest.v1\n"

#: Per-database shape-digest memo, keyed by resolved file path and validated
#: against SQLite's own ``PRAGMA schema_version`` counter — the engine
#: increments it on every DDL statement against the file, so a cached digest
#: is exact, not heuristic: one header-page read per validation, and
#: recomputation happens only when a migration actually ran.
_compat_digest_cache: dict[str, tuple[int, str]] = {}


def _replication_surface() -> dict:
    """The process-constant half of the digest: policy inventory + codec."""
    from .codec import MAGIC

    return {
        "codec": MAGIC.hex(),
        "policies": {
            table: {
                "kind": policy.kind.value,
                "key": list(policy.key),
                "json": sorted(policy.json_columns),
                "excluded": sorted(policy.excluded_columns),
                "timestamps": list(policy.timestamp_columns),
            }
            for table, policy in TABLE_POLICIES.items()
        },
    }


def compatibility_digest(conn: sqlite3.Connection) -> str:
    """Digest of everything that decides whether two machines' delta frames
    are mutually intelligible: the synchronization policy inventory, the
    codec domain identity, and the live logical shape (column name and
    declared type, excluded columns removed) of every replicated table.

    Deliberately absent: the graph ``user_version`` (a proxy for the shape —
    it over-pauses on migrations that never touch replication and
    under-protects against physical drift at equal numbers), the sync
    protocol version (enforced earlier with its own typed error), catalog
    schema versions (local bookkeeping, never wire-visible), and data-phase
    migration progress (captured authored writes propagate through ordinary
    synchronization and need no pause).
    """
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    path = str(conn.execute("PRAGMA database_list").fetchone()[2])
    cached = _compat_digest_cache.get(path) if path else None
    if cached is not None and cached[0] == schema_version:
        return cached[1]
    shape: dict[str, list] = {}
    for table, policy in TABLE_POLICIES.items():
        if policy.kind in {PolicyKind.LOCAL, PolicyKind.DERIVED}:
            continue
        shape[table] = sorted(
            [str(row[1]), str(row[2])]
            for row in conn.execute(f'PRAGMA table_info("{table}")')
            if str(row[1]) not in policy.excluded_columns
        )
    payload = json.dumps(
        {"surface": _replication_surface(), "shape": shape},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(_COMPAT_DIGEST_DOMAIN + payload).hexdigest()
    if path:
        _compat_digest_cache[path] = (schema_version, digest)
    return digest


def audit_schema(conn: sqlite3.Connection) -> dict[str, PolicyKind]:
    """Classify every current user table, failing on silent omissions.

    SQLite's own ``sqlite_*`` tables are implementation state and ignored.
    FTS virtual tables and their shadow tables must still match one of the
    explicit derived prefixes; an unrelated virtual/durable table is not
    waved through.
    """

    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    names = [str(row[0]) for row in rows]
    classified = {name: classify_table(name) for name in names}
    unknown = [name for name, kind in classified.items() if kind is None]
    if unknown:
        raise ValueError("unclassified personal graph tables: " + ", ".join(unknown))
    return {name: kind for name, kind in classified.items() if kind is not None}
