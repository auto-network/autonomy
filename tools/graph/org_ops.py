"""Org registry — filesystem-as-truth + bootstrap orgs table.

Spec: graph://d970d946-f95 (Org Registry & Identity).
Per-org DB decision: graph://7c296600-19b.
Cross-org rules: graph://bcce359d-a1d.

The set of ``data/orgs/*.db`` files IS the org registry. There is no
separate registry file. Adding an org = creating its DB; removing =
deleting the file (after refusing if Settings elsewhere reference the
slug). Each per-org DB carries a single ``orgs`` row that identifies it.

Rich identity (display name, byline, color, favicon) lives as an
``autonomy.org#1`` Setting in the same DB; the schema for that Setting
is defined by auto-S1, so the bootstrap seed here is best-effort —
silently skipped when the schema is unregistered.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from .db import GraphDB
from . import schemas
from .schemas.registry import SchemaValidationError


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ORGS_DIR = REPO_ROOT / "data" / "orgs"

VALID_ORG_TYPES = ("shared", "personal")

ORG_IDENTITY_SET_ID = "autonomy.org"
ORG_IDENTITY_REVISION = 1


# ── Errors ───────────────────────────────────────────────────


class OrgError(Exception):
    """Base class for org registry errors."""


class OrgExistsError(OrgError):
    """Slug already in use (existing DB file)."""


class OrgNotFoundError(OrgError):
    """No DB file for the requested slug."""


class OrgReferencedError(OrgError):
    """Removal refused because Settings elsewhere reference the slug."""

    def __init__(self, slug: str, references: list["CrossRef"]):
        self.slug = slug
        self.references = references
        super().__init__(
            f"cannot remove org {slug!r}: "
            f"{len(references)} cross-DB reference(s) "
            f"(use force=True to override)"
        )


# ── Result types ─────────────────────────────────────────────


@dataclass
class OrgRef:
    """Per-org bootstrap row + filesystem location."""
    id: str
    slug: str
    type: str
    created_at: str
    db_path: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CrossRef:
    """A Setting in a peer org's DB that references the slug being acted on."""
    org: str
    db_path: str
    setting_id: str
    set_id: str
    key: str
    reason: str  # 'key_equals' | 'key_prefix' | 'supersedes' | 'payload_org_field'

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RemovalReport:
    slug: str
    removed: bool
    references: list[CrossRef] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "removed": self.removed,
            "references": [r.to_dict() for r in self.references],
        }


@dataclass
class RenameReport:
    old_slug: str
    new_slug: str
    org: OrgRef
    rewrites: list[CrossRef] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "old_slug": self.old_slug,
            "new_slug": self.new_slug,
            "org": self.org.to_dict(),
            "rewrites": [r.to_dict() for r in self.rewrites],
        }


# ── UUID v7 ──────────────────────────────────────────────────


def uuid7() -> str:
    """Generate a time-ordered UUID v7 in canonical 8-4-4-4-12 hex form.

    48-bit unix-millis timestamp + 74 bits of random + version/variant
    bits per RFC 9562. Time-ordered prefix sorts naturally and stays
    stable across machines for federation.
    """
    ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand = secrets.token_bytes(10)
    b = bytearray(16)
    b[0] = (ts_ms >> 40) & 0xFF
    b[1] = (ts_ms >> 32) & 0xFF
    b[2] = (ts_ms >> 24) & 0xFF
    b[3] = (ts_ms >> 16) & 0xFF
    b[4] = (ts_ms >> 8) & 0xFF
    b[5] = ts_ms & 0xFF
    b[6] = 0x70 | (rand[0] & 0x0F)
    b[7] = rand[1]
    b[8] = 0x80 | (rand[2] & 0x3F)
    b[9] = rand[3]
    b[10:16] = rand[4:10]
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# ── Helpers ──────────────────────────────────────────────────


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _orgs_dir(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root)
    env = os.environ.get("AUTONOMY_ORGS_DIR")
    if env:
        return Path(env)
    return DEFAULT_ORGS_DIR


def _slug_db_path(slug: str, root: Path | str | None = None) -> Path:
    return _orgs_dir(root) / f"{slug}.db"


def _validate_slug(slug: str) -> None:
    if not isinstance(slug, str) or not slug:
        raise OrgError(f"invalid slug: {slug!r}")
    if slug != slug.strip():
        raise OrgError(f"slug must not have surrounding whitespace: {slug!r}")
    if any(c in slug for c in "/\\."):
        raise OrgError(f"slug cannot contain '/', '\\', or '.': {slug!r}")
    if not all(c.isalnum() or c in "-_" for c in slug):
        raise OrgError(
            f"slug must be alphanumeric (with - or _): {slug!r}"
        )
    if slug.startswith("-"):
        raise OrgError(f"slug cannot start with '-': {slug!r}")


def _read_orgs_row(db: GraphDB) -> dict | None:
    try:
        row = db.conn.execute(
            "SELECT id, slug, type, created_at FROM orgs LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def _write_orgs_row(db: GraphDB, *, slug: str, type_: str) -> dict:
    org_id = uuid7()
    created = _now_iso()
    db.conn.execute(
        "INSERT INTO orgs(id, slug, type, created_at) VALUES (?, ?, ?, ?)",
        (org_id, slug, type_, created),
    )
    db.conn.commit()
    return {"id": org_id, "slug": slug, "type": type_, "created_at": created}


def _open_org_db(path: Path) -> GraphDB:
    return GraphDB(path)


# ── Read paths ───────────────────────────────────────────────


def list_orgs(*, root: Path | str | None = None) -> list[OrgRef]:
    """Enumerate orgs by globbing ``data/orgs/*.db``.

    Sorted alphabetically by slug. Files without a bootstrap orgs row
    (legacy or partial) are skipped silently.
    """
    d = _orgs_dir(root)
    if not d.exists():
        return []
    refs: list[OrgRef] = []
    for path in sorted(d.glob("*.db")):
        # Skip WAL/SHM artifacts that glob('*.db') wouldn't match anyway,
        # plus the rare empty/ancillary file.
        try:
            db = _open_org_db(path)
        except sqlite3.Error:
            continue
        try:
            row = _read_orgs_row(db)
        finally:
            db.close()
        if row is None:
            continue
        refs.append(OrgRef(
            id=row["id"], slug=row["slug"], type=row["type"],
            created_at=row["created_at"], db_path=str(path),
        ))
    refs.sort(key=lambda r: r.slug)
    return refs


def get_org(slug: str, *, root: Path | str | None = None) -> OrgRef | None:
    """Return the bootstrap row for *slug*, or ``None`` if absent."""
    path = _slug_db_path(slug, root)
    if not path.exists():
        return None
    try:
        db = _open_org_db(path)
    except sqlite3.Error:
        return None
    try:
        row = _read_orgs_row(db)
    finally:
        db.close()
    if row is None:
        return None
    return OrgRef(
        id=row["id"], slug=row["slug"], type=row["type"],
        created_at=row["created_at"], db_path=str(path),
    )


def show_org(slug: str, *, root: Path | str | None = None) -> dict | None:
    """Return ``{"org": {...}, "identity": {...} | None}`` or ``None``.

    Identity comes from the highest-precedence ``autonomy.org#1`` Setting
    in the org's own DB (canonical wins; absent → None and the cascade
    falls through to the generated fallback at the consumer layer).
    """
    org = get_org(slug, root=root)
    if org is None:
        return None
    db = _open_org_db(Path(org.db_path))
    try:
        row = db.conn.execute(
            "SELECT id, set_id, schema_revision, key, payload, "
            "publication_state, supersedes, excludes, deprecated, "
            "successor_id, created_at, updated_at "
            "FROM settings "
            "WHERE set_id = ? AND key = ? AND excludes IS NULL "
            "ORDER BY "
            "  CASE publication_state "
            "    WHEN 'canonical' THEN 0 "
            "    WHEN 'published' THEN 1 "
            "    WHEN 'curated' THEN 2 "
            "    ELSE 3 END, "
            "  created_at DESC LIMIT 1",
            (ORG_IDENTITY_SET_ID, slug),
        ).fetchone()
    finally:
        db.close()
    identity = None
    if row:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            payload = {}
        identity = {
            "id": row["id"],
            "set_id": row["set_id"],
            "schema_revision": int(row["schema_revision"]),
            "key": row["key"],
            "payload": payload,
            "publication_state": row["publication_state"],
            "supersedes": row["supersedes"],
            "excludes": row["excludes"],
            "deprecated": bool(row["deprecated"]),
            "successor_id": row["successor_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    return {"org": org.to_dict(), "identity": identity}


def find_references(
    slug: str,
    *,
    root: Path | str | None = None,
    exclude_self: bool = True,
) -> list[CrossRef]:
    """Find Settings in peer-org DBs that reference *slug*.

    Reasons reported:
      * ``key_equals``         — Setting keyed exactly by ``slug``
        (e.g. ``autonomy.org`` keyed by org slug,
        ``autonomy.org.peer-subscription`` keyed by caller slug).
      * ``key_prefix``         — composite key starting with ``<slug>:``.
      * ``supersedes``         — override pointing at a Setting that
        lives inside ``<slug>.db``.
      * ``payload_org_field``  — payload's top-level ``org`` field
        equals ``slug``.

    Each reference is reported separately even when multiple reasons fire
    for the same Setting; this lets callers explain to operators exactly
    what would orphan on ``--force``.
    """
    refs: list[CrossRef] = []
    own_path = _slug_db_path(slug, root)

    own_setting_ids: set[str] = set()
    if own_path.exists():
        try:
            db = _open_org_db(own_path)
            try:
                rows = db.conn.execute(
                    "SELECT id FROM settings"
                ).fetchall()
                own_setting_ids = {r["id"] for r in rows}
            finally:
                db.close()
        except sqlite3.OperationalError:
            pass

    d = _orgs_dir(root)
    if not d.exists():
        return refs
    for path in sorted(d.glob("*.db")):
        if exclude_self and path == own_path:
            continue
        try:
            db = _open_org_db(path)
        except sqlite3.Error:
            continue
        try:
            org_row = _read_orgs_row(db)
            org_slug = org_row["slug"] if org_row else path.stem
            try:
                rows = db.conn.execute(
                    "SELECT id, set_id, key, payload, supersedes "
                    "FROM settings"
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for r in rows:
                key = r["key"]
                reasons: list[str] = []
                if isinstance(key, str):
                    if key == slug:
                        reasons.append("key_equals")
                    elif key.startswith(f"{slug}:"):
                        reasons.append("key_prefix")
                if r["supersedes"] and r["supersedes"] in own_setting_ids:
                    reasons.append("supersedes")
                try:
                    payload = json.loads(r["payload"])
                except (json.JSONDecodeError, TypeError):
                    payload = None
                if isinstance(payload, dict) and payload.get("org") == slug:
                    reasons.append("payload_org_field")
                for reason in reasons:
                    refs.append(CrossRef(
                        org=org_slug,
                        db_path=str(path),
                        setting_id=r["id"],
                        set_id=r["set_id"],
                        key=key,
                        reason=reason,
                    ))
        finally:
            db.close()
    return refs


# ── Write paths ──────────────────────────────────────────────


def create_org(
    slug: str,
    *,
    type_: str = "shared",
    identity_payload: dict | None = None,
    identity_state: str = "canonical",
    root: Path | str | None = None,
) -> OrgRef:
    """Create ``data/orgs/<slug>.db`` with bootstrap row + optional seed.

    The seed identity Setting requires ``autonomy.org#1`` to be registered
    in the schema registry (auto-S1's deliverable). When unregistered, the
    seed is silently skipped — the cascade falls through to the generated
    fallback until an operator authors canonical identity later.
    """
    _validate_slug(slug)
    if type_ not in VALID_ORG_TYPES:
        raise OrgError(
            f"invalid type {type_!r}; valid: {VALID_ORG_TYPES}"
        )
    path = _slug_db_path(slug, root)
    if path.exists():
        raise OrgExistsError(
            f"org already exists: {slug} ({path})"
        )
    path.parent.mkdir(parents=True, exist_ok=True)

    db = _open_org_db(path)
    try:
        existing = _read_orgs_row(db)
        if existing is None:
            info = _write_orgs_row(db, slug=slug, type_=type_)
        else:
            info = existing
        if identity_payload is not None:
            _seed_identity_setting(
                db, slug, identity_payload, state=identity_state,
            )
    finally:
        db.close()
    return OrgRef(
        id=info["id"], slug=info["slug"], type=info["type"],
        created_at=info["created_at"], db_path=str(path),
    )


def _seed_identity_setting(
    db: GraphDB,
    slug: str,
    payload: dict,
    *,
    state: str = "canonical",
) -> str | None:
    """Insert ``autonomy.org#1`` Setting; skip silently when schema absent.

    Returns the new Setting id on success, ``None`` when the schema is
    unregistered (auto-S1 owns the schema definition; this bead must
    work whether or not that bead has landed).
    """
    try:
        schemas.validate_payload(
            ORG_IDENTITY_SET_ID, ORG_IDENTITY_REVISION, payload,
        )
    except SchemaValidationError:
        return None
    sid = uuid7()
    now = _now_iso()
    db.conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
        "publication_state, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (sid, ORG_IDENTITY_SET_ID, ORG_IDENTITY_REVISION, slug,
         json.dumps(payload), state, now, now),
    )
    db.conn.commit()
    return sid


def remove_org(
    slug: str,
    *,
    force: bool = False,
    root: Path | str | None = None,
) -> RemovalReport:
    """Delete the org's DB file (and WAL/SHM siblings).

    Refuses with :class:`OrgReferencedError` when peer-org Settings
    reference the slug, unless ``force=True``. The exception carries the
    list of references for the operator. Returns a :class:`RemovalReport`
    describing what happened.
    """
    org = get_org(slug, root=root)
    if org is None:
        raise OrgNotFoundError(f"org not found: {slug}")
    refs = find_references(slug, root=root)
    if refs and not force:
        raise OrgReferencedError(slug, refs)
    path = Path(org.db_path)
    for suffix in ("", "-wal", "-shm"):
        sibling = path.parent / f"{path.name}{suffix}"
        if sibling.exists():
            sibling.unlink()
    return RemovalReport(slug=slug, removed=True, references=refs)


def rename_org(
    slug: str,
    new_slug: str,
    *,
    root: Path | str | None = None,
) -> RenameReport:
    """Move ``data/orgs/<slug>.db`` to ``<new_slug>.db`` and rewrite refs.

    Three-step:
      1. validate, then ``mv`` the file (and WAL/SHM if present),
      2. update the bootstrap orgs row inside the renamed DB,
      3. walk peer DBs and rewrite slug references (keys + payload).

    Bootstrap UUID is preserved.
    """
    _validate_slug(new_slug)
    if slug == new_slug:
        raise OrgError("new slug equals current slug")
    org = get_org(slug, root=root)
    if org is None:
        raise OrgNotFoundError(f"org not found: {slug}")
    if get_org(new_slug, root=root) is not None:
        raise OrgExistsError(f"target slug already exists: {new_slug}")
    refs = find_references(slug, root=root)

    old_path = Path(org.db_path)
    new_path = _slug_db_path(new_slug, root)
    old_path.rename(new_path)
    for suffix in ("-wal", "-shm"):
        side_old = old_path.parent / f"{old_path.name}{suffix}"
        if side_old.exists():
            side_old.rename(new_path.parent / f"{new_path.name}{suffix}")

    db = _open_org_db(new_path)
    try:
        db.conn.execute(
            "UPDATE orgs SET slug = ? WHERE slug = ?", (new_slug, slug),
        )
        _rewrite_slug_in_db(db, slug, new_slug)
        db.conn.commit()
    finally:
        db.close()

    for ref in refs:
        try:
            ref_db = _open_org_db(Path(ref.db_path))
        except sqlite3.Error:
            continue
        try:
            _rewrite_slug_in_db(ref_db, slug, new_slug)
            ref_db.conn.commit()
        finally:
            ref_db.close()

    new_org = get_org(new_slug, root=root)
    assert new_org is not None
    return RenameReport(
        old_slug=slug, new_slug=new_slug, org=new_org, rewrites=refs,
    )


def _rewrite_slug_in_db(db: GraphDB, old: str, new: str) -> None:
    """Rewrite slug references inside a single DB.

    Updates Settings rows whose:
      * ``key`` equals ``old`` (rewrites to ``new``)
      * ``key`` starts with ``f"{old}:"`` (composite key prefix)
      * ``payload`` is a JSON object whose top-level ``org`` equals ``old``
    """
    rows = db.conn.execute(
        "SELECT id, key, payload FROM settings"
    ).fetchall()
    now = _now_iso()
    for r in rows:
        new_key = r["key"]
        if isinstance(r["key"], str):
            if r["key"] == old:
                new_key = new
            elif r["key"].startswith(f"{old}:"):
                new_key = new + r["key"][len(old):]
        try:
            payload = json.loads(r["payload"])
        except (json.JSONDecodeError, TypeError):
            payload = None
        new_payload_str = r["payload"]
        if isinstance(payload, dict) and payload.get("org") == old:
            payload["org"] = new
            new_payload_str = json.dumps(payload)
        if new_key != r["key"] or new_payload_str != r["payload"]:
            db.conn.execute(
                "UPDATE settings SET key = ?, payload = ?, "
                "updated_at = ? WHERE id = ?",
                (new_key, new_payload_str, now, r["id"]),
            )


# ── First-launch bootstrap ───────────────────────────────────


_AUTONOMY_SEED_PAYLOAD: dict[str, Any] = {
    "name": "Autonomy Network",
    "byline": "AGI platform",
    "color": "#6C63FF",
    "favicon": "/static/icon-192.png",
    "type": "shared",
}

_PERSONAL_SEED_PAYLOAD: dict[str, Any] = {
    "name": "Personal",
    "color": "#A0A0A0",
    "type": "personal",
}


def ensure_bootstrap_orgs(
    *,
    root: Path | str | None = None,
) -> list[OrgRef]:
    """Ensure ``autonomy.db`` and ``personal.db`` exist under ``data/orgs/``.

    Idempotent — runs at every dashboard startup; pre-existing DBs are
    left untouched. Identity Setting seed is best-effort (skipped when
    ``autonomy.org#1`` schema is unregistered; auto-S1 owns the schema).

    Returns the list of orgs after bootstrap.
    """
    return [
        _ensure_org("autonomy", "shared", _AUTONOMY_SEED_PAYLOAD, root=root),
        _ensure_org("personal", "personal", _PERSONAL_SEED_PAYLOAD, root=root),
    ]


def _ensure_org(
    slug: str,
    type_: str,
    identity: dict | None,
    *,
    root: Path | str | None = None,
) -> OrgRef:
    existing = get_org(slug, root=root)
    if existing is not None:
        return existing
    return create_org(
        slug, type_=type_, identity_payload=identity, root=root,
    )
