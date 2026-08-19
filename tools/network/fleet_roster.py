"""The fleet roster — a root-signed register of the operator's machines.

Design ``graph://0c655045-ee4`` (auto-0vpse). The roster answers one
question: which machines hold this operator's personal root. It is a
REGISTER, not a ledger — there is one authority (the personal root), it is
ASSERTED rather than derived from a history, there is nobody to defraud but
the root holder, and there are no quorums or windows. So: no chain, no fold,
no witness, no time authority.

**A set of per-machine entries, not a versioned document.** One authority is
not one writer: the root may be exercised at any machine, so there are
several write sites and no serialization point. There is NO roster-level
counter — two offline machines both writing "version 13" is the direct
consequence of having one, so there isn't one. The stored shape is a set of
per-machine entries, each personal-root-signed, and the current roster is a
pure function of that set (:func:`resolve`).

**Merge (an OR-Set with tombstones, per-entry last-writer-wins):**

* Concurrent enrolment of DIFFERENT machines is a union — both root-signed,
  both legitimate, nothing chosen, nothing lost.
* One machine written twice resolves by per-entry ``seq``, ties broken by
  ASCENDING signing-key id — the rule ``storagekit/credentials`` already
  applies to incomparable frontiers.
* A KICK is absorbing against ordinary renewals: it beats a concurrent or
  later renewal regardless of ``seq``, because a renewal must never silently
  un-revoke. It is not permanently absorbing — a re-enrolment that CITES the
  tombstone is the one escape.
* Re-enrolment after a kick CITES the kick it supersedes. That citation is
  what separates a deliberate re-add from an accidental resurrection: without
  it, an old enrol replayed later would resurrect a revoked machine.

**Band ``raw``, never ``published``/``canonical``.** A personal row at those
bands is seen by every organization's read on this operator's machines,
which would put fleet topology into org-scoped surfaces that have no business
with it. ``raw`` keeps the roster local to the fleet, which is its whole
reach.

This module owns the entry model, the personal-root signing, and the merge.
Staleness — knowing a machine is behind — is the sync FRONTIER, which arrives
with the replication piece (``auto-q9ic5``); until then a reader has no
frontier and this module does not pretend one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from tools.network.idkit import KeyPair, canonical_json
from tools.network.idkit.errors import IdkitError, SignatureError
from tools.network.idkit.keys import verify_signature

#: Domain separator — a personal-root signature minted for any other purpose
#: cannot verify as a roster entry, and the version is frozen.
FLEET_ROSTER_DOMAIN = b"autonomy.network.fleet-roster.v1\n"
FLEET_ROSTER_VERSION = 1

#: The settings set the entries are stored under in personal.db, one member
#: per entry, always at ``raw`` band.
FLEET_ROSTER_SET_ID = "autonomy.fleet.roster"


class EntryKind(str, Enum):
    ENROLL = "enroll"   # this machine is in the fleet
    KICK = "kick"       # this machine is revoked (a tombstone)


class FleetRosterError(ValueError):
    """A roster entry is malformed, or does not verify against the anchor."""


@dataclass(frozen=True)
class RosterEntry:
    """One personal-root-signed statement about ONE machine.

    ``machine_pub`` is the machine's authorization (signing) public key — the
    key a roster entry authenticates as authorized (the machine's
    key-distribution/encapsulation address is a SEPARATE record, auto-pw9bs.6,
    under the signing-vs-encryption discipline; it is not here). ``seq`` orders
    repeated statements about one machine; it is per-machine, never a
    roster-wide counter. A KICK carries no ``supersedes``; a re-enrolment after
    a kick MUST set ``supersedes`` to the ``entry_id`` of the kick it revokes.
    """

    personal_root_pub: str  # 64-hex — the fleet anchor and the signer
    machine_pub: str        # 64-hex — the machine's authorization key
    kind: EntryKind
    seq: int                # per-machine sequence
    issued_at: int          # unix ms, informational — NOT a merge input
    supersedes: str | None  # entry_id of a cited kick, for re-enrolment only
    signature: str = field(repr=False)

    @property
    def entry_id(self) -> str:
        """The content id of this entry's signed body — stable, and what a
        re-enrolment cites."""
        import hashlib

        return hashlib.sha256(_body_bytes(self)).hexdigest()


def _body(entry_or_fields) -> dict:
    e = entry_or_fields
    return {
        "v": FLEET_ROSTER_VERSION,
        "personal_root_pub": e.personal_root_pub,
        "machine_pub": e.machine_pub,
        "kind": e.kind.value if isinstance(e.kind, EntryKind) else e.kind,
        "seq": int(e.seq),
        "issued_at": int(e.issued_at),
        "supersedes": e.supersedes,
    }


def _body_bytes(entry) -> bytes:
    return FLEET_ROSTER_DOMAIN + canonical_json(_body(entry))


@dataclass(frozen=True)
class _Unsigned:
    personal_root_pub: str
    machine_pub: str
    kind: EntryKind
    seq: int
    issued_at: int
    supersedes: str | None


def _mint(
    personal_root: KeyPair,
    *,
    machine_pub: str,
    kind: EntryKind,
    seq: int,
    issued_at: int,
    supersedes: str | None,
) -> RosterEntry:
    _require_hex64(personal_root.public_hex, "personal_root_pub")
    _require_hex64(machine_pub, "machine_pub")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise FleetRosterError("roster entry seq must be a non-negative int")
    unsigned = _Unsigned(
        personal_root_pub=personal_root.public_hex, machine_pub=machine_pub,
        kind=kind, seq=seq, issued_at=int(issued_at), supersedes=supersedes,
    )
    sig = personal_root.sign_hex(_body_bytes(unsigned))
    return RosterEntry(
        personal_root_pub=personal_root.public_hex, machine_pub=machine_pub,
        kind=kind, seq=seq, issued_at=int(issued_at), supersedes=supersedes,
        signature=sig,
    )


def enroll(
    personal_root: KeyPair, *, machine_pub: str, seq: int = 0, issued_at: int = 0,
) -> RosterEntry:
    """Add a machine to the fleet. Signed by the personal root; a fresh
    enrolment cites no tombstone."""
    return _mint(
        personal_root, machine_pub=machine_pub, kind=EntryKind.ENROLL,
        seq=seq, issued_at=issued_at, supersedes=None,
    )


def kick(
    personal_root: KeyPair, *, machine_pub: str, seq: int = 0, issued_at: int = 0,
) -> RosterEntry:
    """Revoke a machine — an absorbing tombstone. Beats any concurrent or
    later ordinary renewal; only a tombstone-citing re-enrolment escapes it."""
    return _mint(
        personal_root, machine_pub=machine_pub, kind=EntryKind.KICK,
        seq=seq, issued_at=issued_at, supersedes=None,
    )


def reenroll(
    personal_root: KeyPair,
    *,
    machine_pub: str,
    supersedes: str,
    seq: int = 0,
    issued_at: int = 0,
) -> RosterEntry:
    """Re-add a previously kicked machine. MUST cite the ``entry_id`` of the
    kick it supersedes — the citation is what proves this was written with
    knowledge of the revocation, so a stale enrol replayed later cannot
    resurrect the machine."""
    _require_hex64(supersedes, "supersedes")
    return _mint(
        personal_root, machine_pub=machine_pub, kind=EntryKind.ENROLL,
        seq=seq, issued_at=issued_at, supersedes=supersedes,
    )


def verify(entry: RosterEntry, *, anchor_root_pub: str) -> None:
    """Check the entry is signed by the fleet's personal root. ``anchor_root_pub``
    is the operator's own personal root — an entry carrying or signed by any
    other root is refused, so a foreign roster row cannot merge in."""
    anchor = _require_hex64(anchor_root_pub, "anchor_root_pub")
    if entry.personal_root_pub != anchor:
        raise FleetRosterError(
            f"roster entry anchor {entry.personal_root_pub[:12]}… is not this "
            f"fleet's personal root {anchor[:12]}…"
        )
    if entry.kind == EntryKind.KICK and entry.supersedes is not None:
        raise FleetRosterError("a kick tombstone does not cite a supersedes")
    try:
        verify_signature(entry.personal_root_pub, entry.signature,
                         _body_bytes(entry))
    except SignatureError as exc:
        raise FleetRosterError(
            "roster entry does not verify against its personal root"
        ) from exc
    except IdkitError as exc:
        raise FleetRosterError(f"roster entry anchor is unusable: {exc}") from exc


def _wins(a: RosterEntry, b: RosterEntry) -> RosterEntry:
    """The per-entry last-writer-wins winner between two statements about the
    SAME machine, ignoring kick-absorption (handled in resolve): higher seq
    wins; equal seq breaks by ascending signing-key id — here the entry_id,
    the content hash, matching the ledger's ascending-identifier merge order
    for incomparable frontiers."""
    if a.seq != b.seq:
        return a if a.seq > b.seq else b
    return a if a.entry_id < b.entry_id else b


def resolve(entries, *, anchor_root_pub: str) -> dict[str, RosterEntry]:
    """The current roster: machine_pub -> the ENROLL entry that currently
    holds it, for every machine that is enrolled and not kicked.

    Pure function of the entry set (any order, any duplicates). Verifies every
    entry against the anchor and silently drops the unverifiable — a foreign
    or tampered row never affects the result. Then, per machine:

    * A KICK is absorbing UNLESS a re-enrolment cites it (``supersedes`` =
      that kick's ``entry_id``). An uncited kick revokes the machine.
    * Among ENROLL entries, the last-writer-wins winner holds the slot — but
      only if it is not itself revoked by an uncited kick.
    * A re-enrolment that cites a kick escapes exactly that kick; a fresh
      enrol that cites nothing cannot overcome a kick.
    """
    verified = []
    for e in entries:
        try:
            verify(e, anchor_root_pub=anchor_root_pub)
        except FleetRosterError:
            continue
        verified.append(e)

    by_machine: dict[str, list[RosterEntry]] = {}
    for e in verified:
        by_machine.setdefault(e.machine_pub, []).append(e)

    roster: dict[str, RosterEntry] = {}
    for machine, es in by_machine.items():
        kicks = [e for e in es if e.kind == EntryKind.KICK]
        enrolls = [e for e in es if e.kind == EntryKind.ENROLL]
        # A kick is escaped only by an enrol that explicitly cites it.
        cited = {e.supersedes for e in enrolls if e.supersedes is not None}
        live_kicks = [k for k in kicks if k.entry_id not in cited]
        if live_kicks:
            # Absorbing: the machine is revoked, whatever renewals exist,
            # UNLESS an enrol cites every live kick AND wins LWW over them.
            # An enrol that cites one kick but a later uncited kick exists is
            # still revoked — the newest revocation stands.
            continue
        if not enrolls:
            continue
        winner = enrolls[0]
        for e in enrolls[1:]:
            winner = _wins(winner, e)
        roster[machine] = winner
    return roster


def _entry_payload(entry: RosterEntry) -> dict:
    return {
        "personal_root_pub": entry.personal_root_pub,
        "machine_pub": entry.machine_pub,
        "kind": entry.kind.value,
        "seq": entry.seq,
        "issued_at": entry.issued_at,
        "supersedes": entry.supersedes,
        "signature": entry.signature,
    }


def _entry_from_payload(payload: dict) -> RosterEntry:
    return RosterEntry(
        personal_root_pub=payload["personal_root_pub"],
        machine_pub=payload["machine_pub"],
        kind=EntryKind(payload["kind"]),
        seq=int(payload["seq"]),
        issued_at=int(payload["issued_at"]),
        supersedes=payload.get("supersedes"),
        signature=payload["signature"],
    )


def store_entry(entry: RosterEntry, *, org=None) -> str:
    """Persist one entry as a ``raw`` personal.db row, keyed by its content id
    so every entry is its own member (enrol, kick and re-enrol coexist). The
    schema pins the band to ``raw``; this never writes higher. Returns the
    Setting id."""
    from tools.graph import settings_ops
    from tools.graph.schemas.fleet_roster import FLEET_ROSTER_REVISION

    return settings_ops.add_setting(
        FLEET_ROSTER_SET_ID, FLEET_ROSTER_REVISION, entry.entry_id,
        _entry_payload(entry), org=org, state="raw",
    )


def load_entries(*, org=None) -> list[RosterEntry]:
    """Every stored roster entry from the OWNING store only (personal.db) —
    peer composition is off, because the roster is the operator's own record,
    not a federated view. Returns the raw entry SET for :func:`resolve` /
    :func:`active_machines` to merge; it does not resolve."""
    from tools.graph import settings_ops
    from tools.graph.schemas.fleet_roster import FLEET_ROSTER_REVISION

    members = settings_ops.read_owned_set(
        FLEET_ROSTER_SET_ID, org=org, target_revision=FLEET_ROSTER_REVISION,
    )
    return [_entry_from_payload(m.payload) for m in members]


def current_roster(personal_root_pub: str, *, org=None) -> dict[str, RosterEntry]:
    """The resolved current roster from personal.db: read the entry set, then
    merge it against the operator's anchor."""
    return resolve(load_entries(org=org), anchor_root_pub=personal_root_pub)


def active_machines(entries, *, anchor_root_pub: str) -> set[str]:
    """The set of machine authorization keys currently in the fleet — the
    thin read the sync layer's compaction frontier keys on (auto-q9ic5: one
    checkpoint position per active roster machine). A machine leaves this set
    exactly when an uncited kick revokes it; that is what "remove the kicked
    entry after every peer incorporates the kick" observes."""
    return set(resolve(entries, anchor_root_pub=anchor_root_pub))


def _require_hex64(value, what: str) -> str:
    import re

    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise FleetRosterError(f"roster {what} must be 64 lowercase hex chars")
    return value
