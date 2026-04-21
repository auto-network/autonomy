"""Settings primitive — ops layer.

Read, write, resolve, and migrate operations for the ``settings`` table.
Imported into ``tools.graph.ops`` for unified discovery; tests may reach in
here directly. Spec: graph://0d3f750f-f9c. Cross-org rules:
graph://bcce359d-a1d.

Single-DB world: ``caller_org`` and ``peers`` are plumbed but no-op.
Routing slots in cleanly when per-org DB ships (auto-txg5.x).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any
from uuid import uuid4

from .db import GraphDB, resolve_caller_db_path
from . import schemas


# ── Constants ────────────────────────────────────────────────


VALID_STATES = ("raw", "curated", "published", "canonical")
PRECEDENCE = {"canonical": 0, "published": 1, "curated": 2, "raw": 3}
PEER_VISIBLE_STATES = ("published", "canonical")


# ── Result types ─────────────────────────────────────────────


@dataclass
class ResolvedSetting:
    """A Setting after resolution.

    ``target_revision`` is None when returned at its stored revision (the
    default); otherwise it carries the revision the payload was reshaped to.
    ``org`` is the originating DB's org slug — None in single-DB world.
    """
    id: str
    set_id: str
    stored_revision: int
    key: str
    payload: dict
    publication_state: str
    supersedes: str | None
    excludes: str | None
    deprecated: bool
    successor_id: str | None
    created_at: str
    updated_at: str
    target_revision: int | None = None
    org: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class SetMembers:
    """Result of ``read_set``: resolved members + drop accounting."""
    members: list[ResolvedSetting]
    dropped: dict = field(default_factory=lambda: {
        "below_min_revision": 0,
        "no_upconvert_path": 0,
        "above_target_no_downgrade": 0,
    })

    def to_dict(self) -> dict:
        return {
            "members": [m.to_dict() for m in self.members],
            "dropped": dict(self.dropped),
        }


@dataclass
class MigrationReport:
    """Result of ``migrate_setting_revisions``."""
    set_id: str
    to_revision: int
    dry_run: bool
    rewrote: int = 0
    no_upconvert_path: int = 0
    already_at_target: int = 0
    above_target: int = 0
    affected_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ── DB selection (mirrors ops._open) ─────────────────────────


def _db_path(caller_org: str | None = None) -> str | None:
    """Resolve Settings DB path via the same cascade as ``ops._db_path``.

    Explicit ``caller_org`` wins; otherwise ``GRAPH_ORG`` env is used; the
    resolver applies the scopeless default (``personal``) when neither is
    set. ``GRAPH_DB`` env pins the path regardless (test override).
    """
    env_db = os.environ.get("GRAPH_DB")
    if env_db:
        return env_db
    if caller_org is None:
        caller_org = os.environ.get("GRAPH_ORG")
    return str(resolve_caller_db_path(caller_org))


def _open(caller_org: str | None = None) -> GraphDB:
    return GraphDB(_db_path(caller_org))


# ── JSON merge-patch (RFC 7396) ──────────────────────────────


def json_merge_patch(target: Any, patch: Any) -> Any:
    """Apply RFC 7396 merge-patch.

    - If ``patch`` is not a dict, return it (replace).
    - For dict patches: per key, remove on null; recurse for dict-on-dict;
      otherwise replace.
    - Lists are replaced wholesale, never element-merged.
    """
    if not isinstance(patch, dict):
        return patch
    if not isinstance(target, dict):
        target = {}
    out = dict(target)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = json_merge_patch(out[k], v)
        else:
            out[k] = v
    return out


# ── Row → ResolvedSetting ────────────────────────────────────


def _row_to_resolved(row, *, org: str | None = None,
                     target_revision: int | None = None) -> ResolvedSetting:
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            payload = {}
    return ResolvedSetting(
        id=row["id"],
        set_id=row["set_id"],
        stored_revision=int(row["schema_revision"]),
        key=row["key"],
        payload=payload,
        publication_state=row["publication_state"],
        supersedes=row["supersedes"],
        excludes=row["excludes"],
        deprecated=bool(row["deprecated"]),
        successor_id=row["successor_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        target_revision=target_revision,
        org=org,
    )


# ── Write paths ──────────────────────────────────────────────


def add_setting(
    set_id: str,
    schema_revision: int,
    key: str,
    payload: dict,
    *,
    caller_org: str | None = None,
    state: str = "raw",
) -> str:
    """Create a base Setting in caller_org's DB.

    Validates payload against ``(set_id, schema_revision)``. Returns the new
    Setting id. Raises ``schemas.SchemaValidationError`` on validation
    failure, ``ValueError`` on bad ``state``.
    """
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; valid: {VALID_STATES}")
    schemas.validate_payload(set_id, schema_revision, payload)
    sid = str(uuid4())
    now = _now_iso()
    db = _open(caller_org)
    try:
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (sid, set_id, int(schema_revision), key, json.dumps(payload),
             state, now, now),
        )
        db.conn.commit()
    finally:
        db.close()
    return sid


def override_setting(
    target_id: str,
    payload_overrides: dict,
    *,
    caller_org: str | None = None,
    state: str = "raw",
) -> str:
    """Create a Setting with ``supersedes=target_id`` and partial payload.

    Validation runs against the target's ``(set_id, schema_revision)``
    using the *merged* shape — what consumers will actually see. The
    override Setting itself lives in caller_org's DB; the *target* may
    be either own-org or peer-origin — overriding peer content is the
    expected way to adapt shared primitives to a local org. Raises
    ``LookupError`` only when the target exists nowhere (own or peers).
    """
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; valid: {VALID_STATES}")
    target = _fetch_setting_any_org(target_id, caller_org)
    if target is None:
        raise LookupError(f"override target not found: {target_id!r}")
    db = _open(caller_org)
    try:
        target_payload = json.loads(target["payload"])
        merged = json_merge_patch(target_payload, payload_overrides)
        schemas.validate_payload(
            target["set_id"], int(target["schema_revision"]), merged,
        )
        sid = str(uuid4())
        now = _now_iso()
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, supersedes, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, target["set_id"], int(target["schema_revision"]),
             target["key"], json.dumps(payload_overrides),
             state, target_id, now, now),
        )
        db.conn.commit()
        return sid
    finally:
        db.close()


def exclude_setting(
    target_id: str,
    *,
    caller_org: str | None = None,
    state: str = "raw",
) -> str:
    """Create a Setting with ``excludes=target_id`` and empty payload.

    The exclude row itself lives in caller_org's DB and only affects
    reads scoped to this caller — so peer-origin targets are allowed.
    """
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; valid: {VALID_STATES}")
    target = _fetch_setting_any_org(target_id, caller_org)
    if target is None:
        raise LookupError(f"exclude target not found: {target_id!r}")
    db = _open(caller_org)
    try:
        sid = str(uuid4())
        now = _now_iso()
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, excludes, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, target["set_id"], int(target["schema_revision"]),
             target["key"], "{}", state, target_id, now, now),
        )
        db.conn.commit()
        return sid
    finally:
        db.close()


def _reject_peer_setting_target(
    setting_id: str,
    caller_org: str | None,
) -> None:
    """Raise :class:`ops.CrossOrgWriteError` when ``setting_id`` lives in a peer DB.

    Lookup order mirrors :func:`_fetch_setting_any_org`, but instead of
    returning the row we raise so promote/deprecate/remove fail fast
    with the structured cross-org error rather than a bare LookupError.
    """
    from .cross_org import open_peer_db, resolve_peers
    from .ops import CrossOrgWriteError  # local: avoid import cycle at top

    resolved_org = _resolve_settings_caller(caller_org)
    for peer in sorted(resolve_peers(resolved_org, None)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        row = peer_db.conn.execute(
            "SELECT 1 FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        if row is not None:
            raise CrossOrgWriteError(setting_id, peer)


def promote_setting(
    setting_id: str,
    to_state: str,
    *,
    caller_org: str | None = None,
) -> None:
    """Transition publication_state. ``LookupError`` if not present.

    Peer-origin targets raise :class:`ops.CrossOrgWriteError` — only the
    origin org may alter a Setting's publication state.
    """
    if to_state not in VALID_STATES:
        raise ValueError(f"invalid state {to_state!r}; valid: {VALID_STATES}")
    now = _now_iso()
    db = _open(caller_org)
    try:
        cur = db.conn.execute(
            "UPDATE settings SET publication_state = ?, updated_at = ? "
            "WHERE id = ?",
            (to_state, now, setting_id),
        )
        if cur.rowcount == 0:
            _reject_peer_setting_target(setting_id, caller_org)
            raise LookupError(f"setting not found: {setting_id!r}")
        db.conn.commit()
    finally:
        db.close()


def deprecate_setting(
    setting_id: str,
    *,
    caller_org: str | None = None,
    successor_id: str | None = None,
) -> None:
    """Mark a Setting deprecated, optionally pointing at a successor.

    Peer-origin targets raise :class:`ops.CrossOrgWriteError`.
    """
    now = _now_iso()
    db = _open(caller_org)
    try:
        cur = db.conn.execute(
            "UPDATE settings SET deprecated = 1, successor_id = ?, "
            "updated_at = ? WHERE id = ?",
            (successor_id, now, setting_id),
        )
        if cur.rowcount == 0:
            _reject_peer_setting_target(setting_id, caller_org)
            raise LookupError(f"setting not found: {setting_id!r}")
        db.conn.commit()
    finally:
        db.close()


def remove_setting(
    setting_id: str,
    *,
    caller_org: str | None = None,
) -> None:
    """Hard-delete a Setting. Spec restricts to ``raw``; higher states must
    be deprecated first.

    Peer-origin targets raise :class:`ops.CrossOrgWriteError`.
    """
    db = _open(caller_org)
    try:
        row = db.conn.execute(
            "SELECT publication_state FROM settings WHERE id = ?",
            (setting_id,),
        ).fetchone()
        if not row:
            _reject_peer_setting_target(setting_id, caller_org)
            raise LookupError(f"setting not found: {setting_id!r}")
        if row["publication_state"] != "raw":
            raise ValueError(
                f"can only remove raw Settings; this is "
                f"{row['publication_state']!r} — deprecate first"
            )
        db.conn.execute("DELETE FROM settings WHERE id = ?", (setting_id,))
        db.conn.commit()
    finally:
        db.close()


# ── Read paths ───────────────────────────────────────────────


def list_set_ids(
    *,
    caller_org: str | None = None,
    peers: list[str] | None = None,
) -> list[str]:
    """Distinct ``set_id`` values visible to caller_org.

    Own DB contributes every ``set_id``; peer DBs contribute only
    ``set_id`` values backed by a ``published``/``canonical`` row. See
    graph://bcce359d-a1d § External view.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    seen: set[str] = set()
    db = _open(caller_org)
    try:
        rows = db.conn.execute(
            "SELECT DISTINCT set_id FROM settings"
        ).fetchall()
        for r in rows:
            if r[0]:
                seen.add(r[0])
    finally:
        db.close()

    resolved_org = _resolve_settings_caller(caller_org)
    for peer in sorted(resolve_peers(resolved_org, peers)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        placeholders = ",".join("?" for _ in PEER_VISIBLE_STATES)
        rows = peer_db.conn.execute(
            f"SELECT DISTINCT set_id FROM settings "
            f"WHERE publication_state IN ({placeholders}) "
            f"  AND excludes IS NULL AND deprecated = 0",
            list(PEER_VISIBLE_STATES),
        ).fetchall()
        for r in rows:
            if r[0]:
                seen.add(r[0])
    return sorted(seen)


def _resolve_settings_caller(caller_org: str | None) -> str | None:
    """Mirror of ``ops._resolve_caller_org`` avoiding the import cycle.

    Returns the concrete slug a caller should be treated as when
    resolving peer reads — explicit > ``GRAPH_ORG`` > ``None`` (triggers
    the scopeless default inside ``resolve_caller_db_path``).
    """
    if caller_org is not None:
        return caller_org
    return os.environ.get("GRAPH_ORG")


def _fetch_setting_any_org(
    setting_id: str,
    caller_org: str | None,
) -> dict | None:
    """Return the Setting row as a plain dict, searching own-org then peers.

    Peer rows must satisfy the public-surface filter
    (``publication_state IN ('published','canonical')``). Used by
    override/exclude targets, which are allowed to reference peer
    content — the *override row* itself still lands in caller_org's DB.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    db = _open(caller_org)
    try:
        row = db.conn.execute(
            "SELECT * FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        if row is not None:
            return dict(row)
    finally:
        db.close()

    resolved_org = _resolve_settings_caller(caller_org)
    for peer in sorted(resolve_peers(resolved_org, None)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        row = peer_db.conn.execute(
            "SELECT * FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        if row is not None and row["publication_state"] in PEER_VISIBLE_STATES:
            return dict(row)
    return None


def get_setting(
    setting_id: str,
    *,
    caller_org: str | None = None,
    peers: list[str] | None = None,
    target_revision: int | None = None,
) -> ResolvedSetting | None:
    """Resolve a single Setting by id, own-first then peers.

    ``None`` if not found or dropped by revision constraints. Peer rows
    only surface when their ``publication_state`` is ``published`` or
    ``canonical``.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    resolved_org = _resolve_settings_caller(caller_org)
    db = _open(caller_org)
    try:
        row = db.conn.execute(
            "SELECT * FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        if row:
            resolved = _row_to_resolved(row, org=resolved_org)
    finally:
        db.close()

    if 'resolved' not in locals():
        resolved = None
        for peer in sorted(resolve_peers(resolved_org, peers)):
            peer_db = open_peer_db(peer)
            if peer_db is None:
                continue
            row = peer_db.conn.execute(
                "SELECT * FROM settings WHERE id = ?", (setting_id,)
            ).fetchone()
            if row and row["publication_state"] in PEER_VISIBLE_STATES:
                resolved = _row_to_resolved(row, org=peer)
                break
        if resolved is None:
            return None

    if target_revision is None:
        return resolved
    transformed, reason = _shape_to_target(resolved, target_revision)
    if transformed is None:
        return None
    return transformed


def read_set(
    set_id: str,
    *,
    caller_org: str | None = None,
    peers: list[str] | None = None,
    target_revision: int | None = None,
    min_revision: int | None = None,
) -> SetMembers:
    """Resolve members of *set_id* visible to caller_org's session.

    Five-step pipeline (see graph://0d3f750f-f9c § Resolution algorithm):
    1. per-DB fetch (single DB today, peers loop ready),
    2. group by key into bases / overrides / exclusions,
    3. drop excluded bases,
    4. pick highest-precedence base per key (tie-break: most recent),
    5. apply overrides via JSON-merge-patch.

    Optional ``min_revision`` filters before transform; ``target_revision``
    upconverts (or drops if no chain). Returns a ``SetMembers`` with drop
    accounting populated.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    resolved_org = _resolve_settings_caller(caller_org)
    raw_rows: list[tuple[str | None, Any]] = []
    db = _open(caller_org)
    try:
        rows = db.conn.execute(
            "SELECT * FROM settings WHERE set_id = ?", (set_id,)
        ).fetchall()
        for r in rows:
            raw_rows.append((resolved_org, r))
    finally:
        db.close()

    # Peer-org contributions: public surface only.
    resolved_peers = resolve_peers(resolved_org, peers)
    for peer in sorted(resolved_peers):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        placeholders = ",".join("?" for _ in PEER_VISIBLE_STATES)
        rows = peer_db.conn.execute(
            f"SELECT * FROM settings WHERE set_id = ? "
            f"  AND publication_state IN ({placeholders})",
            (set_id, *PEER_VISIBLE_STATES),
        ).fetchall()
        for r in rows:
            raw_rows.append((peer, r))

    dropped = {
        "below_min_revision": 0,
        "no_upconvert_path": 0,
        "above_target_no_downgrade": 0,
    }

    # Apply min_revision floor before transform (drops live rows wholesale).
    survivors: list[tuple[str | None, Any]] = []
    for org, row in raw_rows:
        if min_revision is not None and int(row["schema_revision"]) < min_revision:
            dropped["below_min_revision"] += 1
            continue
        survivors.append((org, row))

    # Group by key.
    bases: dict[str, list[tuple[str | None, Any]]] = {}
    overrides: dict[str, list[tuple[str | None, Any]]] = {}
    excludes: dict[str, list[tuple[str | None, Any]]] = {}
    for org, row in survivors:
        bucket = (
            "excludes" if row["excludes"] is not None
            else "overrides" if row["supersedes"] is not None
            else "bases"
        )
        target = (excludes if bucket == "excludes"
                  else overrides if bucket == "overrides"
                  else bases)
        target.setdefault(row["key"], []).append((org, row))

    members: list[ResolvedSetting] = []
    keys_seen = sorted(bases.keys())
    for key in keys_seen:
        excluded_ids = {row["excludes"] for (_, row) in excludes.get(key, [])}
        candidate_bases = [
            (org, row) for (org, row) in bases[key]
            if row["id"] not in excluded_ids
        ]
        if not candidate_bases:
            continue

        # Pick highest precedence; tie-break by most recent created_at.
        # Two-pass stable sort: recency first, then precedence wins.
        candidate_bases.sort(key=lambda om: om[1]["created_at"] or "", reverse=True)
        candidate_bases.sort(
            key=lambda om: PRECEDENCE.get(om[1]["publication_state"], 99),
        )
        chosen_org, chosen_row = candidate_bases[0]

        # Apply overrides whose supersedes targets this base.
        merged_payload = json.loads(chosen_row["payload"])
        for (_, ov_row) in overrides.get(key, []):
            if ov_row["supersedes"] == chosen_row["id"]:
                ov_payload = json.loads(ov_row["payload"])
                merged_payload = json_merge_patch(merged_payload, ov_payload)

        resolved = _row_to_resolved(chosen_row, org=chosen_org)
        resolved.payload = merged_payload

        # Optional revision transform.
        if target_revision is not None:
            transformed, reason = _shape_to_target(resolved, target_revision)
            if transformed is None:
                dropped[reason] += 1
                continue
            members.append(transformed)
        else:
            members.append(resolved)

    return SetMembers(members=members, dropped=dropped)


# ── Schema versioning helpers ────────────────────────────────


def _shape_to_target(
    resolved: ResolvedSetting,
    target_revision: int,
) -> tuple[ResolvedSetting | None, str]:
    """Return (transformed, '') on success, or (None, reason) on drop.

    Identity case copies through. Lower stored → upconvert via registry chain.
    Higher stored → drop with reason ``above_target_no_downgrade``.
    Missing chain → drop with reason ``no_upconvert_path``.
    """
    stored = resolved.stored_revision
    if stored == target_revision:
        out = ResolvedSetting(**{**asdict(resolved), "target_revision": target_revision})
        return out, ""
    if stored > target_revision:
        return None, "above_target_no_downgrade"
    converted = schemas.upconvert_payload(
        resolved.set_id, stored, target_revision, resolved.payload,
    )
    if converted is None:
        return None, "no_upconvert_path"
    out = ResolvedSetting(**{**asdict(resolved), "payload": converted,
                              "target_revision": target_revision})
    return out, ""


# ── Storage migration ────────────────────────────────────────


def migrate_setting_revisions(
    set_id: str,
    to_revision: int,
    *,
    caller_org: str | None = None,
    dry_run: bool = False,
) -> MigrationReport:
    """Rewrite stored rows at lower revisions up to ``to_revision``.

    Optional housekeeping — read-time upconvert keeps things working without
    it. Rows already at ``to_revision`` are skipped; rows above it are left
    alone (downgrades are explicit opt-ins, not part of migrate). Rows with
    no upconvert chain are reported, not rewritten.
    """
    report = MigrationReport(
        set_id=set_id, to_revision=int(to_revision), dry_run=dry_run,
    )
    now = _now_iso()
    db = _open(caller_org)
    try:
        rows = db.conn.execute(
            "SELECT id, schema_revision, payload FROM settings "
            "WHERE set_id = ? AND excludes IS NULL",
            (set_id,),
        ).fetchall()
        for row in rows:
            stored = int(row["schema_revision"])
            if stored == to_revision:
                report.already_at_target += 1
                continue
            if stored > to_revision:
                report.above_target += 1
                continue
            payload = json.loads(row["payload"])
            converted = schemas.upconvert_payload(
                set_id, stored, to_revision, payload,
            )
            if converted is None:
                report.no_upconvert_path += 1
                continue
            report.affected_ids.append(row["id"])
            report.rewrote += 1
            if not dry_run:
                db.conn.execute(
                    "UPDATE settings SET payload = ?, schema_revision = ?, "
                    "updated_at = ? WHERE id = ?",
                    (json.dumps(converted), int(to_revision), now, row["id"]),
                )
        if not dry_run:
            db.conn.commit()
    finally:
        db.close()
    return report


# ── Helpers ──────────────────────────────────────────────────


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
