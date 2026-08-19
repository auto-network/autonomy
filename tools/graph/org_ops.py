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

from tools.data_paths import DATA_ROOT, resolve_orgs_root

from .db import GraphDB
from . import schemas
from .schemas.registry import SchemaValidationError


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ORGS_DIR = DATA_ROOT / "orgs"

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
    return resolve_orgs_root(root, default=DEFAULT_ORGS_DIR)


def _slug_db_path(slug: str, root: Path | str | None = None) -> Path:
    # Routed through db's constructor so the two reserved local-store
    # names resolve to their own home outside data/orgs/ (auto-35kmy) —
    # a second, unrouted copy here is how the personal store would get
    # re-created at the legacy path on a fresh install.
    from .db import _org_db_path

    return _org_db_path(slug, root)


def _validate_slug(slug: str, *, allow_local_store: bool = False) -> None:
    if not isinstance(slug, str) or not slug:
        raise OrgError(f"invalid slug: {slug!r}")
    from .db import LOCAL_STORE_SLUGS

    if slug in LOCAL_STORE_SLUGS and not allow_local_store:
        # Reserved (auto-35kmy): routing is by string, and one string
        # cannot name two stores. After an ORGANIZATION named "personal"
        # existed, org="personal" would have to mean the operator's store
        # to keep every existing call site working AND the new org's store
        # to reach it — no path helper can tell which the caller meant.
        raise OrgError(
            f"{slug!r} is reserved for the operator's local store and "
            f"cannot name an organization"
        )
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


def _open_org_db(path: Path) -> GraphDB:
    return GraphDB(path)


# ── Read paths ───────────────────────────────────────────────


def list_orgs(*, root: Path | str | None = None) -> list[OrgRef]:
    """Enumerate orgs by globbing ``data/orgs/*.db``.

    Sorted alphabetically by slug. Files without a bootstrap orgs row
    (legacy or partial) are skipped silently.
    """
    d = _orgs_dir(root)
    refs: list[OrgRef] = []
    candidate_paths = sorted(d.glob("*.db")) if d.exists() else []
    # The operator's local stores live BESIDE the orgs directory
    # (auto-35kmy) and are walked by name: they are not organizations, but
    # this is the operator's store inventory, and the TYPE column is what
    # tells them apart.
    from .db import LOCAL_STORE_SLUGS, _local_store_db_path

    for name in LOCAL_STORE_SLUGS:
        local = _local_store_db_path(name, root)
        if local.exists() and local not in candidate_paths:
            candidate_paths.append(local)
    for path in candidate_paths:
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
    # References live in the operator's LOCAL stores too — a personal
    # override keyed by this org's slug is exactly the reference this scan
    # exists to surface — and those stores are no longer in the orgs glob
    # (auto-35kmy), so they are walked by name.
    from .db import LOCAL_STORE_SLUGS, _local_store_db_path

    candidate_paths = sorted(d.glob("*.db"))
    for name in LOCAL_STORE_SLUGS:
        local = _local_store_db_path(name, root)
        if local.exists() and local not in candidate_paths:
            candidate_paths.append(local)
    for path in candidate_paths:
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

    Delegates DB creation + schema init + bootstrap orgs-row insertion to
    :meth:`GraphDB.create_org_db`; the identity Setting seed layers on top.

    The seed identity Setting requires ``autonomy.org#1`` to be registered
    in the schema registry (auto-S1's deliverable). When unregistered, the
    seed is silently skipped — the cascade falls through to the generated
    fallback until an operator authors canonical identity later.
    """
    # The one legitimate reserved-name creation is provisioning the
    # operator's own store: slug "personal" with the "personal" type.
    _validate_slug(slug, allow_local_store=(slug == "personal" and type_ == "personal"))
    if type_ not in VALID_ORG_TYPES:
        raise OrgError(
            f"invalid type {type_!r}; valid: {VALID_ORG_TYPES}"
        )
    path = _slug_db_path(slug, root)
    if path.exists():
        raise OrgExistsError(
            f"org already exists: {slug} ({path})"
        )

    try:
        db = GraphDB.create_org_db(slug, type_=type_, path=path)
    except FileExistsError as e:
        raise OrgExistsError(str(e)) from e
    try:
        info = _read_orgs_row(db)
        assert info is not None, "create_org_db did not seed bootstrap row"
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


def create_org_shell(
    slug: str,
    *,
    type_: str = "shared",
    identity_payload: dict | None = None,
    identity_state: str = "canonical",
    root: Path | str | None = None,
) -> OrgRef:
    """Create the organization SHELL for the browser founding ceremony (I1).

    The shell is the org DB + slug + optional identity/branding, with NO
    password, NO server-side founding, and NO key seal: the org root is
    generated and the four founding events are signed in the operator's browser,
    then folded via ``POST /api/network/ledger/found`` with the sealed org-key
    submitted separately. The no-passphrase counterpart to
    :func:`create_org_with_identity` (which founds server-side under a password).

    Idempotent on an UN-FOUNDED shell (the two-step failure window, auto-jdba4):
    if the org already exists but its ledger has not been folded, the existing
    shell is returned so the browser can retry the ceremony against it. A shell
    whose ledger is already founded is a real conflict (:class:`OrgExistsError`).
    A failed founding therefore never strands a half-org that cannot be
    completed.
    """
    from tools.network.ledger.store import LedgerStore, org_ledger_db_path

    _validate_slug(slug)
    if type_ not in VALID_ORG_TYPES:
        raise OrgError(f"invalid type {type_!r}; valid: {VALID_ORG_TYPES}")

    path = _slug_db_path(slug, root)
    if not path.exists():
        return create_org(
            slug, type_=type_, identity_payload=identity_payload,
            identity_state=identity_state, root=root,
        )

    # Existing org: reusable as a retry target only if its ledger is UN-FOUNDED.
    ledger_path = org_ledger_db_path(slug, root)
    founded = False
    if ledger_path.exists():
        try:
            with LedgerStore(ledger_path) as store:
                founded = len(store) > 0
        except Exception:
            founded = True  # unreadable ledger -> treat as founded; refuse.
    if founded:
        raise OrgExistsError(f"org already exists and is founded: {slug}")

    existing = get_org(slug, root=root)
    if existing is None:
        raise OrgError(f"org shell {slug!r} vanished during shell-create")
    return existing


@dataclass(frozen=True)
class OrgCeremonyResult:
    """What the create-organization ceremony minted (auto-nixfv)."""

    org: OrgRef
    root_pub: str
    genesis_id: str
    founder_persona_pub: str
    event_ids: tuple  # the four founding events, in append order

    def to_dict(self) -> dict:
        return {
            "org": self.org.to_dict(),
            "identity": {
                "root_pub": self.root_pub,
                "genesis_id": self.genesis_id,
                "founder_persona_pub": self.founder_persona_pub,
                "event_ids": list(self.event_ids),
            },
        }


def _personal_identity_member():
    """The enrolled personal identity Setting — canonical ``default`` label
    first, first member as the legacy fallback (mirrors identity_routes)."""
    from . import settings_ops
    from .schemas.personal_identity import PERSONAL_IDENTITY_SET_ID

    members = [
        m
        for m in settings_ops.read_owned_set(PERSONAL_IDENTITY_SET_ID, org=None).members
        if isinstance(m.payload, dict)
    ]
    for m in members:
        if m.key == "default":
            return m
    return members[0] if members else None


def create_org_with_identity(
    slug: str,
    personal_password: str,
    *,
    type_: str = "shared",
    identity_payload: dict | None = None,
    root: Path | str | None = None,
    now: int | None = None,
    recovery_pub: str | None = None,
) -> OrgCeremonyResult:
    """Create an organization as one atomic founding ceremony (auto-nixfv).

    Unlock the personal root (wrong password fails HERE, before any
    filesystem effect) → create the org DB (mints the stable ``orgs.id``)
    → mint an independent org signing root → found the ledger (genesis,
    ``owner`` role, key-bound founding invite, founder claim — D-01/D20;
    ``genesis.org`` carries the stable ``orgs.id`` UUID label, never the
    slug and never a registry identifier, D21) → seal the org-root seed
    to the owner's personal-root-derived X25519 key (B4 Option B) and
    persist it as org-key revision 2. On any failure after DB creation,
    the org is deleted and the exception re-raised: an organization is
    observable only fully founded and key-sealed. No registration and no
    registry identifier at creation.
    """
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import decrypt_root_key
    from tools.network.ledger import LedgerStore, org_ledger_db_path
    from tools.network.ledger.found import found_org_ledger
    from tools.network.storagekit import credentials as _credentials

    member = _personal_identity_member()
    if member is None or not member.payload.get("armored_private_key"):
        raise OrgError(
            "no personal identity is enrolled; enroll one before creating "
            "an organization (creation founds the ledger under your key)"
        )
    # Step 1 — unlock first: a wrong password raises ArmorPassphraseError
    # here, before anything exists on disk.
    personal_kp = decrypt_root_key(
        member.payload["armored_private_key"], personal_password
    )
    personal_seed = bytes.fromhex(personal_kp.private_hex)

    ref = create_org(
        slug, type_=type_, identity_payload=identity_payload, root=root
    )
    try:
        org_root = KeyPair.generate()  # independent of the personal root
        now_ms = int(time.time() * 1000) if now is None else now
        store = LedgerStore(org_ledger_db_path(slug, root))
        try:
            founded = found_org_ledger(
                store,
                org_id=ref.id,  # the stable orgs.id UUID label (D21)
                org_root=org_root,
                personal_root_seed=personal_seed,
                now=now_ms,
                # Contract §5 closed allowance (auto-uh2dp): the founding
                # claim always carries a PersonaKemCredential, so the founder
                # has a grant address the moment a later member advances the
                # state after an access contraction. The seed derives from the
                # personal root, not a device (§1c); a second machine re-derives
                # it and opens the same grants with no new key-control record.
                kem_seed=_credentials.derive_kem_seed(personal_seed),
                # The recovery-code ceremony's PUBLIC half only (derived from the
                # cold code in the operator's own context) -- declaring it at
                # genesis relocates no secret; recovery_pub == root_pub is
                # rejected by _v_genesis (make_event validates it).
                recovery_pub=recovery_pub,
            )
        finally:
            store.close()
        _seal_org_root_setting(slug, org_root, personal_seed)
        _record_persona_setting(
            slug, founded.genesis_id, founded.founder_persona_pub, source="found"
        )
        return OrgCeremonyResult(
            org=ref,
            root_pub=org_root.public_hex,
            genesis_id=founded.genesis_id,
            founder_persona_pub=founded.founder_persona_pub,
            event_ids=(
                founded.genesis_id,
                founded.role_define_id,
                founded.founding_invite_id,
                founded.founder_claim_id,
            ),
        )
    except BaseException:
        # Atomic boundary: a half-run leaves nothing observable.
        try:
            GraphDB.close_all_pooled()
            remove_org(slug, force=True, root=root)
        except Exception:
            pass
        raise


def _record_persona_setting(
    slug: str,
    genesis_id: str,
    persona_pub: str,
    *,
    source: str,
    invite_ref: str | None = None,
) -> None:
    """Write down which persona this node holds the seed for in *slug*.

    Called at the only moments the value exists without a passphrase: the
    founding ceremony and the join claim. The ledger already records that this
    persona is a member; what it cannot say is which member is us, because
    that depends on who holds which seed.

    ``org=None`` — deliberately, and this is the load-bearing part. That
    resolves to personal.db, this operator's own database. Writing it into the
    org's shared DB would put two members' different personas on one
    (set_id, key, org) row, and each would then read the other's identity.
    """
    from . import settings_ops
    from .schemas.network_identity import (
        NETWORK_PERSONA_REVISION,
        NETWORK_PERSONA_SET_ID,
    )

    payload = {
        "persona_pub": persona_pub,
        "derived_at": _now_iso(),
        "source": source,
    }
    if invite_ref:
        payload["invite_ref"] = invite_ref

    existing = _persona_member(genesis_id)
    if existing is not None:
        stored = existing.payload.get("persona_pub")
        if stored and stored != persona_pub:
            # Deterministic derivation means this cannot happen from the same
            # seed and the same genesis. It means the seed changed underneath
            # us, and silently overwriting would strand every record already
            # attributed to the old persona.
            raise ValueError(
                f"persona for genesis {genesis_id[:16]}… is already recorded as "
                f"{stored[:16]}…, refusing to overwrite with {persona_pub[:16]}…"
            )
        settings_ops.upsert_by_key(
            NETWORK_PERSONA_SET_ID, NETWORK_PERSONA_REVISION, genesis_id,
            payload, org=None,
        )
        return
    settings_ops.add_setting(
        NETWORK_PERSONA_SET_ID, NETWORK_PERSONA_REVISION, genesis_id,
        payload, org=None, state="raw",
    )


def _persona_member(genesis_id: str):
    """The recorded persona row for one genesis, or None."""
    from . import settings_ops
    from .schemas.network_identity import NETWORK_PERSONA_SET_ID

    for member in settings_ops.read_owned_set(
        NETWORK_PERSONA_SET_ID, org=None
    ).members:
        if member.key == genesis_id and isinstance(member.payload, dict):
            return member
    return None


#: Process-lifetime read-through cache for persona_pub_for_org. A hit can
#: never become wrong: persona_pub is the stable member id bound at claim
#: time, fixed forever for a genesis id (a rekey moves the signing key and
#: preserves the member id; a re-founded org has a new genesis and so is a
#: different entry). Misses are NOT cached — the found/join ceremony writes
#: the row that turns a miss into a hit while the process runs, and a
#: cached miss would leave locality silently wrong until restart.
_persona_pub_cache: dict[str, str] = {}


def persona_pub_for_org(genesis_id: str) -> str | None:
    """This node's persona public key in the org with *genesis_id*, or None.

    Takes the genesis id rather than a slug because the genesis IS the
    identifier — a caller that does not know which genesis it means is asking
    an ambiguous question and must not be handed a guess. Resolve a slug to
    its genesis by folding that org's ledger.

    No side effects: never derives, never prompts, never touches the seed.
    Returning None means "not recorded on this node", never "you are not a
    member" — membership is the ledger's answer to give, not this row's.

    Read-through: one personal.db read per genesis id per process, ever
    (see ``_persona_pub_cache`` above for why a hit is safe forever and a
    miss is never cached).
    """
    cached = _persona_pub_cache.get(genesis_id)
    if cached is not None:
        return cached
    member = _persona_member(genesis_id)
    if member is None:
        return None
    pub = member.payload.get("persona_pub")
    if isinstance(pub, str) and pub:
        _persona_pub_cache[genesis_id] = pub
        return pub
    return None


def _seal_org_root_setting(slug: str, org_root, personal_seed: bytes) -> None:
    """Seal *org_root*'s seed to the owner's derived X25519 key and persist
    the org-key revision-2 Setting (B4 Option B) — shared by the creation
    ceremony and the retrofit's keyless path."""
    from tools.network.idkit.sealing import derive_encapsulation_keypair, seal

    from . import settings_ops
    from .schemas.network_identity import (
        NETWORK_ORG_KEY_REVISION_2,
        NETWORK_ORG_KEY_SET_ID,
        ORG_ROOT_ARMOR_PURPOSE,
    )

    _, recipient_pub = derive_encapsulation_keypair(
        personal_seed, ORG_ROOT_ARMOR_PURPOSE
    )
    sealed = seal(
        bytes.fromhex(org_root.private_hex), recipient_pub, ORG_ROOT_ARMOR_PURPOSE
    )
    settings_ops.add_setting(
        NETWORK_ORG_KEY_SET_ID,
        NETWORK_ORG_KEY_REVISION_2,
        "default",
        {
            "root_pub": org_root.public_hex,
            "sealed_root_key": sealed.hex(),
            "owner_kem_pub": recipient_pub,
            "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
        },
        org=slug,
        # raw keeps the sealed root key off the cross-org read-through
        # surface: published/canonical rows are what peer orgs compose
        # against, so a canonical org key is readable by every subscribing
        # org. Every reader of this set uses read_owned_set (peers=[]),
        # which returns all states from the owning DB, so raw costs them
        # nothing. Matches the sibling writers, which already default raw.
        state="raw",
    )


#: What the org root signs to authorise re-sealing itself to a new recipient.
#: A distinct domain, so this signature can never be mistaken for a ledger one.
ORG_KEY_RESEAL_DOMAIN = b"autonomy.org-key.reseal.v1\n"


def _validate_sealed_org_key_payload(sealed_payload: dict) -> None:
    """Exact shape of a revision-2 sealed org key. Shared by store and reseal."""
    from .schemas.network_identity import ORG_ROOT_ARMOR_PURPOSE

    if not isinstance(sealed_payload, dict):
        raise OrgError("sealed org-key payload must be an object")
    required = ("root_pub", "sealed_root_key", "owner_kem_pub", "seal_purpose")
    missing = [
        k for k in required
        if not isinstance(sealed_payload.get(k), str) or not sealed_payload.get(k)
    ]
    if missing:
        raise OrgError(f"sealed org-key payload missing/empty fields: {missing}")
    for k in ("root_pub", "sealed_root_key", "owner_kem_pub"):
        try:
            bytes.fromhex(sealed_payload[k])
        except ValueError as e:
            raise OrgError(f"sealed org-key field {k!r} must be hex") from e
    if sealed_payload["seal_purpose"] != ORG_ROOT_ARMOR_PURPOSE:
        raise OrgError(
            f"seal_purpose must be {ORG_ROOT_ARMOR_PURPOSE!r}, "
            f"got {sealed_payload['seal_purpose']!r}"
        )


def reseal_input(slug: str, sealed_payload: dict) -> bytes:
    """The binding the org root signs to authorise a re-seal.

    Bound to the org, the unchanged root, and the exact new recipient and
    ciphertext, so an authorisation for one re-seal authorises no other.
    """
    from tools.network.idkit.canonical import canonical_json

    return ORG_KEY_RESEAL_DOMAIN + canonical_json(
        {
            "slug": slug,
            "root_pub": sealed_payload["root_pub"],
            "owner_kem_pub": sealed_payload["owner_kem_pub"],
            "sealed_root_key": sealed_payload["sealed_root_key"],
        }
    )


def reseal_org_key(slug: str, sealed_payload: dict, signature_hex: str) -> None:
    """Re-seal a FOUNDED org's root key to a new recipient (auto-t1nek).

    When an owner rotates their personal root, every seal made to the old one
    stops opening for them --- including their own organization's root key.
    They are locked out of something they still own, and the thief who holds
    the old personal root is not.

    This is a RE-WRAP, not a replacement: the org root itself is unchanged, so
    the ledger's commitment to it is untouched. That distinction is enforced
    rather than trusted --- a payload naming a different ``root_pub`` is
    refused, which is what keeps the founding-time lock meaningful.

    The server cannot open the seal, so it cannot tell a genuine re-wrap from
    a blob sealed to an attacker's key. Anyone could otherwise overwrite the
    stored seal and lock the owner out for good. So the ORG ROOT signs the
    exact new payload: only a caller who can already open the org root can
    change who it opens for, which grants no power they did not have.
    """
    from tools.network.idkit.keys import verify_signature

    _validate_sealed_org_key_payload(sealed_payload)
    if get_org(slug) is None:
        raise OrgNotFoundError(f"org does not exist: {slug}")

    current = _current_sealed_org_key(slug)
    if current is None:
        raise OrgError(
            f"org {slug} has no sealed root key to re-seal; store one first"
        )
    if sealed_payload["root_pub"] != current["root_pub"]:
        raise OrgError(
            "a re-seal may not change the org root key: this organization's "
            f"ledger has committed to {current['root_pub'][:16]}...; replacing "
            "the key itself is a ledger rotation, not a re-seal"
        )
    verify_signature(
        current["root_pub"], signature_hex, reseal_input(slug, sealed_payload)
    )

    from . import settings_ops
    from .schemas.network_identity import (
        NETWORK_ORG_KEY_REVISION_2,
        NETWORK_ORG_KEY_SET_ID,
        ORG_ROOT_ARMOR_PURPOSE,
    )

    settings_ops.upsert_by_key(
        NETWORK_ORG_KEY_SET_ID,
        NETWORK_ORG_KEY_REVISION_2,
        "default",
        {
            "root_pub": sealed_payload["root_pub"],
            "sealed_root_key": sealed_payload["sealed_root_key"],
            "owner_kem_pub": sealed_payload["owner_kem_pub"],
            "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
        },
        org=slug,
    )


def _current_sealed_org_key(slug: str) -> dict | None:
    """The org's stored revision-2 sealed root key payload, or None.

    Asks for revision 2 rather than inspecting payloads to work out which
    revision a row is. Both revisions live under the same key -- the stored-row
    uniqueness constraint is ``(set_id, schema_revision, key,
    publication_state)``, so no key can separate them -- and a revision-1 row
    has no upconvert path to 2 by design (``NetworkOrgKeyV2``), so targeting the
    revision drops it as ``no_upconvert_path`` rather than reshaping it. That
    absence of an upconvert chain is exactly what makes revision-targeting the
    right selector here.

    Do NOT generalise this to every reader of the set: only a caller with a
    revision-specific intent should target one. ``get_org_key`` deliberately
    serves either generation to the browser (auto-05tom), and the create-time
    existence check and the ``root_pub`` match are both revision-agnostic.

    KNOWN SUBSTRATE DEFECT, not fixed here. When a revision-1 row and a
    revision-2 row both exist under this key, resolution collapses to one row
    and revision 1 wins, so the sealed key is unreachable: untargeted reads
    return revision 1, and ``target_revision=2`` drops revision 1 as
    ``no_upconvert_path`` and returns nothing. Payload inspection does not help
    either -- the revision-2 payload is never in the result set to inspect --
    so this function returned ``None`` in that case before this change too.
    The consequence is that ``store_sealed_org_key`` refuses a re-seal for
    exactly the organizations that have one, which is the revision-1-to-2
    retrofit path. Reproduction:
    ``/workspace/output/repro-org-key-revision-collapse.py``. Owned by the
    settings substrate, not by this module.
    """
    from . import settings_ops
    from .schemas.network_identity import (
        NETWORK_ORG_KEY_REVISION_2,
        NETWORK_ORG_KEY_SET_ID,
    )

    members = list(settings_ops.read_owned_set(
        NETWORK_ORG_KEY_SET_ID,
        org=slug,
        target_revision=NETWORK_ORG_KEY_REVISION_2,
    ))
    for member in members:
        if isinstance(member.payload, dict):
            return member.payload
    return None


def store_sealed_org_key(slug: str, sealed_payload: dict) -> None:
    """Persist an org root key SEALED IN THE BROWSER (I1) — the client-driven
    counterpart to :func:`_seal_org_root_setting`. The browser generates the org
    root and seals it to the owner's derived encapsulation key locally, then
    submits only the sealed material: no root plaintext and no passphrase ever
    reach the server.

    Stored at ``raw`` (a secret's home): the sealed org key is owner-local and
    must never reach the cross-org read-through surface. Every reader is
    owning-DB-only (``read_owned_set``), so raw is both correct and reader-safe
    — the legacy server-side seal wrote ``canonical`` (harmless only because no
    federated reader exists; the browser path does it right).

    Idempotent (upsert): the seal-first-then-fold retry path may resubmit the
    same sealed key after a founding that failed before the fold, so resubmitting
    must be a no-op.
    """
    from . import settings_ops
    from .schemas.network_identity import (
        NETWORK_ORG_KEY_REVISION_2,
        NETWORK_ORG_KEY_SET_ID,
        ORG_ROOT_ARMOR_PURPOSE,
    )

    _validate_sealed_org_key_payload(sealed_payload)
    if get_org(slug) is None:
        raise OrgNotFoundError(f"org shell does not exist: {slug}")

    # The org root key is LOCKED at founding: idempotent (re)store is allowed
    # only while the ledger is UN-FOUNDED — the seal-first-then-fold window and
    # its retries. Once founded, the ledger has committed to this root and the
    # key can never be replaced (mirrors put_org_key's no-overwrite protection,
    # scoped to the founding window so the retry path still works).
    from tools.network.ledger.store import LedgerStore, org_ledger_db_path

    ledger_path = org_ledger_db_path(slug)
    founded = False
    if ledger_path.exists():
        try:
            with LedgerStore(ledger_path) as store:
                founded = len(store) > 0
        except Exception:
            founded = True  # unreadable ledger -> treat as founded; refuse.
    if founded:
        raise OrgExistsError(
            f"org {slug} is founded — its root key is locked and cannot be replaced"
        )

    settings_ops.upsert_by_key(
        NETWORK_ORG_KEY_SET_ID,
        NETWORK_ORG_KEY_REVISION_2,
        "default",
        {
            "root_pub": sealed_payload["root_pub"],
            "sealed_root_key": sealed_payload["sealed_root_key"],
            "owner_kem_pub": sealed_payload["owner_kem_pub"],
            "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
        },
        org=slug,
    )


@dataclass
class RetrofitReport:
    """Per-org outcomes of :func:`retrofit_found_ledgers`."""

    outcomes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"outcomes": list(self.outcomes)}


def _resolve_org_root_for_retrofit(slug: str, personal_password: str, personal_seed: bytes):
    """The org's signing root from its stored key Setting, or a fresh mint.

    Returns ``(org_root, minted)``. Revision-1 legacy armor opens with the
    personal password; revision-2 opens with the owner's derived recipient
    key; no Setting at all mints a fresh independent root (persisted,
    sealed, by the caller). A key that will not open aborts THIS org.
    """
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import decrypt_root_key
    from tools.network.idkit.sealing import derive_encapsulation_keypair
    from tools.network.idkit.sealing import open as seal_open

    from . import settings_ops
    from .schemas.network_identity import (
        NETWORK_ORG_KEY_SET_ID,
        ORG_ROOT_ARMOR_PURPOSE,
    )

    members = [
        m
        for m in settings_ops.read_owned_set(NETWORK_ORG_KEY_SET_ID, org=slug).members
        if isinstance(m.payload, dict)
    ]
    if not members:
        return KeyPair.generate(), True
    payload = members[0].payload
    if payload.get("sealed_root_key"):
        recipient_priv, _ = derive_encapsulation_keypair(
            personal_seed, ORG_ROOT_ARMOR_PURPOSE
        )
        seed = seal_open(
            bytes.fromhex(payload["sealed_root_key"]),
            recipient_priv,
            payload.get("seal_purpose", ORG_ROOT_ARMOR_PURPOSE),
        )
        return KeyPair.from_private_hex(seed.hex()), False
    return decrypt_root_key(payload["armored_private_key"], personal_password), False


def retrofit_found_ledgers(
    personal_password: str,
    *,
    root: Path | str | None = None,
    now: int | None = None,
) -> RetrofitReport:
    """Found the authority ledger for every existing organization.

    One-time, idempotent (auto-6l3f8): the personal password unlocks the
    owner's root ONCE, up front — a wrong password aborts before any
    organization is touched. Per org: a founded ledger is skipped; a
    keyed org founds under its stored root (legacy password armor or the
    revision-2 seal); a keyless org first mints an independent root
    sealed to the owner (Option B) and then founds identically; a
    partial founding is COMPLETED onto its existing genesis through the
    guarded resume (the only identity-preserving recovery). An org whose
    key will not open is recorded and left unfounded; the run continues.
    """
    from tools.network.idkit.armor import decrypt_root_key
    from tools.network.ledger import LedgerStore, org_ledger_db_path
    from tools.network.ledger.found import found_org_ledger, resume_org_founding
    from tools.network.storagekit import credentials as _credentials

    member = _personal_identity_member()
    if member is None or not member.payload.get("armored_private_key"):
        raise OrgError(
            "no personal identity is enrolled; the retrofit founds ledgers "
            "under your personal key"
        )
    personal_kp = decrypt_root_key(
        member.payload["armored_private_key"], personal_password
    )  # wrong password raises HERE, before any org is touched
    personal_seed = bytes.fromhex(personal_kp.private_hex)
    now_ms = int(time.time() * 1000) if now is None else now

    report = RetrofitReport()
    for org in list_orgs(root=root):
        if org.slug == "personal":
            continue
        entry = {"slug": org.slug, "outcome": None, "genesis_id": None}
        report.outcomes.append(entry)
        try:
            store = LedgerStore(org_ledger_db_path(org.slug, root))
            try:
                state = store.fold() if store.ledger.genesis_id else None
                if state is not None and any(
                    "owner" in m.roles for m in state.members.values()
                ):
                    entry["outcome"] = "skipped_already_founded"
                    entry["genesis_id"] = state.genesis_id
                    continue
                org_root, minted = _resolve_org_root_for_retrofit(
                    org.slug, personal_password, personal_seed
                )
                if minted:
                    _seal_org_root_setting(org.slug, org_root, personal_seed)
                if store.ledger.genesis_id is None:
                    founded = found_org_ledger(
                        store,
                        org_id=org.id,
                        org_root=org_root,
                        personal_root_seed=personal_seed,
                        now=now_ms,
                        # Same closed §5 allowance as the fresh founding
                        # (auto-uh2dp): every founder claim carries a
                        # PersonaKemCredential, retrofit included.
                        kem_seed=_credentials.derive_kem_seed(personal_seed),
                    )
                else:  # interrupted prior run: guarded, identity-preserving
                    founded = resume_org_founding(
                        store,
                        org_id=org.id,
                        org_root=org_root,
                        personal_root_seed=personal_seed,
                        kem_seed=_credentials.derive_kem_seed(personal_seed),
                    )
                store.refresh_projections()
                entry["outcome"] = "keyed_and_founded" if minted else "founded"
                entry["genesis_id"] = founded.genesis_id
                # Covers BOTH arms above: a fresh founding and a resumed one
                # return the same FoundedLedger shape, so the record is
                # written once here rather than duplicated in each branch.
                _record_persona_setting(
                    org.slug, founded.genesis_id, founded.founder_persona_pub,
                    source="found",
                )
                entry["persona"] = founded.founder_persona_pub
            finally:
                store.close()
        except Exception as e:  # abort THIS org, name it, continue the run
            entry["outcome"] = f"error: {e}"
    return report


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
    expires_at = schemas.cache_expires_at(
        ORG_IDENTITY_SET_ID, ORG_IDENTITY_REVISION, now,
    )
    db.conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
        "publication_state, created_at, updated_at, expires_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (sid, ORG_IDENTITY_SET_ID, ORG_IDENTITY_REVISION, slug,
         json.dumps(payload), state, now, now, expires_at),
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
        "SELECT id, key, payload, set_id, schema_revision FROM settings"
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
            expires_at = schemas.cache_expires_at(
                r["set_id"], int(r["schema_revision"]), now,
            )
            db.conn.execute(
                "UPDATE settings SET key = ?, payload = ?, "
                "updated_at = ?, expires_at = ? WHERE id = ?",
                (new_key, new_payload_str, now, expires_at, r["id"]),
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


# First-run env overrides (H3, graph://dc310166-911): a fresh deployment
# names its own first shared org instead of inheriting "autonomy".
FIRST_ORG_ENV = "AUTONOMY_FIRST_ORG"
FIRST_ORG_NAME_ENV = "AUTONOMY_FIRST_ORG_NAME"


def resolve_first_org_slug(slug: str | None = None) -> str:
    """First-org slug resolution: explicit arg > env > ``autonomy``."""
    return slug or os.environ.get(FIRST_ORG_ENV) or "autonomy"


def _first_org_seed(slug: str, display_name: str | None) -> dict[str, Any]:
    if slug == "autonomy" and display_name is None:
        return _AUTONOMY_SEED_PAYLOAD
    name = (
        display_name
        or os.environ.get(FIRST_ORG_NAME_ENV)
        or slug.replace("-", " ").replace("_", " ").title()
    )
    return {"name": name, "type": "shared"}


def ensure_bootstrap_orgs(
    *,
    root: Path | str | None = None,
    first_org: str | None = None,
    first_org_name: str | None = None,
    personal_only: bool = False,
) -> list[OrgRef]:
    """Ensure the first shared org (under ``data/orgs/``) and the personal
    store (``data/personal.db``, beside it — auto-35kmy) exist.

    ``personal_only`` creates just the operator's own store and founds no
    shared org — the invite-join path (auto-8v5ri), where membership
    arrives from the INVITING org's ledger rather than from a local
    creation.

    The first org defaults to ``autonomy`` (this host's historical
    behavior) but a fresh deployment names its own: pass ``first_org``
    explicitly, or set ``AUTONOMY_FIRST_ORG`` (display name via
    ``AUTONOMY_FIRST_ORG_NAME``) before first launch.

    Idempotent — runs at every dashboard startup; pre-existing DBs are
    left untouched. Identity Setting seed is best-effort (skipped when
    ``autonomy.org#1`` schema is unregistered; auto-S1 owns the schema).

    When neither the arg nor the env names a first org, an existing
    shared org satisfies the bootstrap — startup never manufactures a
    default ``autonomy`` org next to one the operator already created
    (e.g. via ``python -m tools.init --org acme``).

    Returns the list of orgs after bootstrap.
    """
    from .db import relocate_local_stores

    # The bootstrap is where the process is about to own the stores it
    # would move, so the one-time relocation of the local stores out of
    # data/orgs/ runs here (auto-35kmy) — dashboard startup and first-run
    # both pass through, and nothing else ever moves a live file.
    relocate_local_stores(root)
    if personal_only:
        return [_ensure_org("personal", "personal", _PERSONAL_SEED_PAYLOAD, root=root)]
    slug = first_org or os.environ.get(FIRST_ORG_ENV)
    if slug is None:
        shared = [o for o in list_orgs(root=root) if o.type == "shared"]
        if shared:
            return shared + [
                _ensure_org("personal", "personal", _PERSONAL_SEED_PAYLOAD, root=root),
            ]
        slug = "autonomy"
    _validate_slug(slug)
    return [
        _ensure_org(slug, "shared", _first_org_seed(slug, first_org_name), root=root),
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
