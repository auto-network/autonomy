"""The fleet ORG roster — which organizations the operator belongs to, as
synced state, so every fleet machine materialises the same org databases.

Sibling in spirit to :mod:`tools.network.fleet_roster` (which records MACHINES),
but deliberately SIMPLER: it is NOT personal-root-signed. The machine roster is
signed because a machine entry authorises a box to receive the entire fleet and
is delivered out-of-band during enrollment, before that machine is trusted, so
it must self-verify against the anchor. The org roster only ever travels INSIDE
the already-authenticated personal-scope sync (whose trust boundary is the
signed machine roster), and a bogus entry's worst case is a harmless empty stub
(home only serves real org content). So this follows the ``_record_persona_setting``
precedent: an unsigned, server-written, synced personal.db record — no seed, no
ceremony, no browser step.

Why it exists: org databases live at ``data/orgs/<slug>.db`` and were only ever
*implied* from which files happened to exist locally. A fresh fleet member has
none, so ``discover_org_sync_scopes`` found nothing and the org databases never
synchronised — the bootstrap chicken-and-egg. With this roster, the member reads
the org set (carried by the personal-scope sync), materialises each
``orgs/<slug>.db`` stub with the recorded ``org_id`` (so it is the SAME org, not
merely the same slug), and the existing org-DB sync engine fills it.

Entries are content-addressed and coexist (enrol / kick / kick-citing re-enrol)
so the OR-set merge in :func:`resolve` sees them all — the same merge shape as
the machine roster, minus the signature.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum

from tools.network.idkit import canonical_json

FLEET_ORG_ROSTER_VERSION = 1

#: The settings set the entries are stored under in personal.db, one org per
#: entry, always at ``raw`` band (kept off org surfaces; carried across the
#: fleet by the personal-scope sync).
FLEET_ORG_ROSTER_SET_ID = "autonomy.fleet.org-roster"

#: Domain separator for the content id (NOT a signature — the entry is unsigned;
#: this only namespaces the hash so an id cannot collide with another record's).
FLEET_ORG_ROSTER_DOMAIN = b"autonomy.fleet.org-roster-entry.v1\n"


class EntryKind(str, Enum):
    ENROLL = "enroll"   # the operator belongs to this org
    KICK = "kick"       # the org is removed from the fleet (a tombstone)


class FleetOrgRosterError(ValueError):
    """An org-roster entry is malformed."""


def _require_nonempty(value: str, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise FleetOrgRosterError(f"{what} must be a non-empty string")
    return value


@dataclass(frozen=True)
class OrgRosterEntry:
    """One statement about ONE organization in the operator's fleet.

    ``org_slug`` is the sync scope name (the org DB is ``orgs/<slug>.db``) and
    the merge identity. ``org_id`` is the stable ``orgs.id`` UUID (the ledger
    genesis org label) — a member materialises the stub with THIS id so both
    machines are the same org, not merely the same slug. ``seq`` orders repeated
    statements about one org; a re-enrol after a kick MUST cite the kick's
    ``entry_id`` in ``supersedes``.
    """

    org_slug: str
    org_id: str
    kind: EntryKind
    seq: int
    issued_at: int
    supersedes: str | None = None

    @property
    def entry_id(self) -> str:
        import hashlib

        return hashlib.sha256(
            FLEET_ORG_ROSTER_DOMAIN + canonical_json(self.to_dict())
        ).hexdigest()

    def to_dict(self) -> dict:
        return {
            "v": FLEET_ORG_ROSTER_VERSION,
            "org_slug": self.org_slug,
            "org_id": self.org_id,
            "kind": self.kind.value if isinstance(self.kind, EntryKind) else self.kind,
            "seq": self.seq,
            "issued_at": self.issued_at,
            "supersedes": self.supersedes,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "OrgRosterEntry":
        if not isinstance(payload, dict):
            raise FleetOrgRosterError("org roster entry must be a dict")
        expected = {"v", *(f.name for f in fields(cls))}
        extra = set(payload) - expected
        if extra:
            raise FleetOrgRosterError(f"org roster entry has unknown fields: {sorted(extra)}")
        try:
            kind = EntryKind(payload.get("kind"))
        except ValueError as exc:
            raise FleetOrgRosterError(
                f"org roster entry kind {payload.get('kind')!r} is unknown"
            ) from exc
        return cls(
            org_slug=payload.get("org_slug"),
            org_id=payload.get("org_id"),
            kind=kind,
            seq=payload.get("seq"),
            issued_at=payload.get("issued_at"),
            supersedes=payload.get("supersedes"),
        )


def _make(
    *, org_slug: str, org_id: str, kind: EntryKind, seq: int, issued_at: int,
    supersedes: str | None,
) -> OrgRosterEntry:
    _require_nonempty(org_slug, "org_slug")
    _require_nonempty(org_id, "org_id")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise FleetOrgRosterError("org roster entry seq must be a non-negative int")
    if not isinstance(issued_at, int) or isinstance(issued_at, bool) or issued_at < 0:
        raise FleetOrgRosterError("org roster entry issued_at must be a non-negative int")
    if kind == EntryKind.KICK and supersedes is not None:
        raise FleetOrgRosterError("a kick tombstone does not cite a supersedes")
    return OrgRosterEntry(
        org_slug=org_slug, org_id=org_id, kind=kind, seq=int(seq),
        issued_at=int(issued_at), supersedes=supersedes,
    )


def enroll(*, org_slug: str, org_id: str, seq: int = 0, issued_at: int = 0) -> OrgRosterEntry:
    """Record that the operator belongs to an org. A fresh enrolment cites no
    tombstone."""
    return _make(
        org_slug=org_slug, org_id=org_id, kind=EntryKind.ENROLL,
        seq=seq, issued_at=issued_at, supersedes=None,
    )


def kick(*, org_slug: str, org_id: str, seq: int = 0, issued_at: int = 0) -> OrgRosterEntry:
    """Remove an org from the fleet — an absorbing tombstone. Only a
    tombstone-citing re-enrolment escapes it."""
    return _make(
        org_slug=org_slug, org_id=org_id, kind=EntryKind.KICK,
        seq=seq, issued_at=issued_at, supersedes=None,
    )


def reenroll(
    *, org_slug: str, org_id: str, supersedes: str, seq: int = 0, issued_at: int = 0,
) -> OrgRosterEntry:
    """Re-add a previously kicked org. MUST cite the ``entry_id`` of the kick it
    supersedes, so a stale enrol replayed later cannot resurrect it."""
    _require_nonempty(supersedes, "supersedes")
    return _make(
        org_slug=org_slug, org_id=org_id, kind=EntryKind.ENROLL,
        seq=seq, issued_at=issued_at, supersedes=supersedes,
    )


def _wins(a: OrgRosterEntry, b: OrgRosterEntry) -> OrgRosterEntry:
    """Per-entry last-writer-wins between two statements about the SAME org:
    higher seq wins; equal seq breaks by ascending content id."""
    if a.seq != b.seq:
        return a if a.seq > b.seq else b
    return a if a.entry_id < b.entry_id else b


def resolve(entries) -> dict[str, OrgRosterEntry]:
    """The current org roster: org_slug -> the ENROLL entry that currently holds
    it, for every org enrolled and not kicked.

    Pure function of the entry set (any order, any duplicates). Malformed entries
    are dropped. Kick is absorbing unless a re-enrolment cites it; among enrols,
    LWW wins.
    """
    valid = []
    for e in entries:
        try:
            _require_nonempty(e.org_slug, "org_slug")
            _require_nonempty(e.org_id, "org_id")
        except FleetOrgRosterError:
            continue
        valid.append(e)

    by_org: dict[str, list[OrgRosterEntry]] = {}
    for e in valid:
        by_org.setdefault(e.org_slug, []).append(e)

    roster: dict[str, OrgRosterEntry] = {}
    for org_slug, es in by_org.items():
        kicks = [e for e in es if e.kind == EntryKind.KICK]
        enrolls = [e for e in es if e.kind == EntryKind.ENROLL]
        cited = {e.supersedes for e in enrolls if e.supersedes is not None}
        live_kicks = [k for k in kicks if k.entry_id not in cited]
        if live_kicks:
            continue
        if not enrolls:
            continue
        winner = enrolls[0]
        for e in enrolls[1:]:
            winner = _wins(winner, e)
        roster[org_slug] = winner
    return roster


def _entry_payload(entry: OrgRosterEntry) -> dict:
    payload = entry.to_dict()
    payload.pop("v")
    return payload


def _entry_from_payload(payload: dict) -> OrgRosterEntry:
    return OrgRosterEntry.from_dict({"v": FLEET_ORG_ROSTER_VERSION, **payload})


def store_entry(entry: OrgRosterEntry, *, org=None) -> str:
    """Persist one entry as a ``raw`` personal.db row, keyed by its content id.
    Idempotent upsert; needs no seed. Returns the Setting id."""
    from tools.graph import settings_ops
    from tools.graph.schemas.fleet_org_roster import FLEET_ORG_ROSTER_REVISION

    return settings_ops.upsert_by_key(
        FLEET_ORG_ROSTER_SET_ID, FLEET_ORG_ROSTER_REVISION, entry.entry_id,
        _entry_payload(entry), org=org, state="raw",
    )


def load_entries(*, org=None) -> list[OrgRosterEntry]:
    """Every stored org-roster entry from the OWNING store only (personal.db).
    Returns the raw entry SET for :func:`resolve` to merge; does not resolve."""
    from tools.graph import settings_ops
    from tools.graph.schemas.fleet_org_roster import FLEET_ORG_ROSTER_REVISION

    members = settings_ops.read_owned_set(
        FLEET_ORG_ROSTER_SET_ID, org=org, target_revision=FLEET_ORG_ROSTER_REVISION,
    )
    return [_entry_from_payload(m.payload) for m in members]


def current_orgs(*, org=None) -> dict[str, OrgRosterEntry]:
    """The resolved current org roster from personal.db: read the set, resolve."""
    return resolve(load_entries(org=org))


def publish_org(org_slug: str, org_id: str, *, org=None, now_ms: int | None = None) -> bool:
    """Ensure an org is in the roster. Idempotent — if the resolved roster
    already holds this slug, nothing is written. Returns True if it wrote a new
    enrol. Server-side, no seed."""
    import time

    if org_slug in current_orgs(org=org):
        return False
    issued = int(time.time() * 1000) if now_ms is None else now_ms
    store_entry(enroll(org_slug=org_slug, org_id=org_id, issued_at=issued), org=org)
    return True
