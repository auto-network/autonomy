"""Secret subgroups: a group event log folded against the parent roster.

A subgroup is a DERIVED FOLD, never a frozen snapshot (design of record
``graph://fe4499fa-0e9``, operator-ratified 2026-08-22): effective
membership is

    replay(group events)  ∩  domain_member_keys(parent fold)

recomputed at read time. The group log stores persona references plus
add/remove events — never captured credentials — and joins the parent
ledger BY REFERENCE AT FOLD TIME. Three properties follow and are pinned
by tests: temporal independence (a person who joined the organization
after the charter can still be added), cascade for free (removal from
the organization removes the person from every group instantly, because
membership is an intersection, not a copy), and the invariant that a
group member who is not an organization member is unrepresentable.

Write-authority is the admission predicate (design Decision 3): version
one implements ``admin-set`` — only a charter-declared admin's signature
makes a valid add/remove; anyone else's record "is simply not a valid
group event" and is skipped at replay, never applied. Content
decryption is the grant set: :func:`group_member_credentials` mirrors
``_current_member_credentials`` (``tools/vault/key_sealer.py``) so the
generation/grant/re-mint machinery is unchanged — only the recipient
set differs (design Decision 2, the single seam).

Version-one boundaries, each deliberate:

- ``public`` visibility only: the log lives beside the org's data and
  the parent roster check runs centrally at replay. The ``private``
  placement (log sealed to members) changes storage, not this fold.
- Charter acceptance requires the author to be a current organization
  member. The delegated group-creation capability (charter auditable
  against an org-root cert) is the tightening tracked by auto-tjd1c.
- Persona references intersect on the members' CURRENT keys as the
  parent fold reports them. Following a ``member.rekey`` lineage from a
  charter-era key to its successor is a documented follow-up; nothing
  here stores a credential, so rekey never strands ciphertext.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from tools.network.idkit import KeyPair, canonical_json
from tools.network.idkit import verify_signature as idkit_verify_signature
from tools.network.idkit.errors import IdkitError

from .credentials import domain_member_keys, select_current_credential
from .errors import MalformedRecordError, RecordSignatureError
from .records import record_id, signing_input

GROUP_VERSION = 1
CHARTER_DOMAIN = b"autonomy.group.charter.v1\n"
EVENT_DOMAIN = b"autonomy.group.event.v1\n"

_ID_HEX_LEN = 64
_KEY_HEX_LEN = 64
_OPS = ("add", "remove")
# Safety-biased tie: at an identical HLC, a remove replays after an add
# for the same subject and therefore wins — mirroring the parent fold's
# concurrent-revoke-outranks-grant rule.
_OP_RANK = {"add": 0, "remove": 1}


def _require_hex(value: object, length: int, what: str) -> str:
    if not isinstance(value, str) or len(value) != length:
        raise MalformedRecordError(f"{what} must be {length} hex chars")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise MalformedRecordError(f"{what} is not hex") from exc
    return value


def _require_hlc(value: object, what: str) -> tuple:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or not all(isinstance(x, int) and x >= 0 for x in value)
    ):
        raise MalformedRecordError(f"{what} must be [ts_ms, count] non-negative ints")
    return (value[0], value[1])


@dataclass(frozen=True)
class GroupCharter:
    """The founding record: who may write, starting from whom."""

    version: int
    group_id: str
    name: str
    visibility: str
    admission: str
    admins: tuple
    initial_members: tuple
    author: str
    created_hlc: tuple
    signature: str

    def _payload(self, *, signed: bool) -> dict:
        payload = {
            "version": self.version,
            "group_id": self.group_id,
            "name": self.name,
            "visibility": self.visibility,
            "admission": self.admission,
            "admins": list(self.admins),
            "initial_members": list(self.initial_members),
            "author": self.author,
            "created_hlc": list(self.created_hlc),
        }
        if signed:
            payload["signature"] = self.signature
        return payload

    def signing_input(self) -> bytes:
        return signing_input(CHARTER_DOMAIN, self._payload(signed=False))

    def wire(self) -> bytes:
        return canonical_json(self._payload(signed=True))

    @property
    def charter_id(self) -> str:
        return record_id(self.wire())


@dataclass(frozen=True)
class GroupEvent:
    """One membership change, valid only under its charter's predicate."""

    version: int
    group_id: str
    charter_id: str
    op: str
    subject: str
    author: str
    hlc: tuple
    signature: str

    def _payload(self, *, signed: bool) -> dict:
        payload = {
            "version": self.version,
            "group_id": self.group_id,
            "charter_id": self.charter_id,
            "op": self.op,
            "subject": self.subject,
            "author": self.author,
            "hlc": list(self.hlc),
        }
        if signed:
            payload["signature"] = self.signature
        return payload

    def signing_input(self) -> bytes:
        return signing_input(EVENT_DOMAIN, self._payload(signed=False))

    def wire(self) -> bytes:
        return canonical_json(self._payload(signed=True))

    @property
    def event_id(self) -> str:
        return record_id(self.wire())


def create_charter(
    author: KeyPair,
    group_id: str,
    name: str,
    admins,
    initial_members,
    created_hlc,
) -> GroupCharter:
    _require_hex(group_id, _ID_HEX_LEN, "group_id")
    admins = tuple(admins)
    initial_members = tuple(initial_members)
    if not admins:
        raise MalformedRecordError("charter must declare at least one admin")
    for key in (*admins, *initial_members):
        _require_hex(key, _KEY_HEX_LEN, "persona key")
    if not isinstance(name, str) or not name:
        raise MalformedRecordError("charter name must be a non-empty string")
    unsigned = GroupCharter(
        version=GROUP_VERSION,
        group_id=group_id,
        name=name,
        visibility="public",
        admission="admin-set",
        admins=admins,
        initial_members=initial_members,
        author=author.public_hex,
        created_hlc=_require_hlc(created_hlc, "created_hlc"),
        signature="",
    )
    signature = author.sign(unsigned.signing_input()).hex()
    return GroupCharter(**{**_as_dict(unsigned), "signature": signature})


def make_group_event(
    author: KeyPair,
    charter: GroupCharter,
    op: str,
    subject: str,
    hlc,
) -> GroupEvent:
    if op not in _OPS:
        raise MalformedRecordError(f"op must be one of {_OPS}")
    _require_hex(subject, _KEY_HEX_LEN, "subject")
    unsigned = GroupEvent(
        version=GROUP_VERSION,
        group_id=charter.group_id,
        charter_id=charter.charter_id,
        op=op,
        subject=subject,
        author=author.public_hex,
        hlc=_require_hlc(hlc, "hlc"),
        signature="",
    )
    signature = author.sign(unsigned.signing_input()).hex()
    return GroupEvent(**{**_as_dict(unsigned), "signature": signature})


def _as_dict(record) -> dict:
    return {f.name: getattr(record, f.name) for f in fields(record)}


def verify_charter(charter: GroupCharter) -> None:
    """Structural and signature validity; authority is checked separately."""
    if charter.version != GROUP_VERSION:
        raise MalformedRecordError("unsupported charter version")
    if charter.visibility != "public":
        raise MalformedRecordError("version one accepts only public visibility")
    if charter.admission != "admin-set":
        raise MalformedRecordError("version one accepts only the admin-set predicate")
    _require_hex(charter.group_id, _ID_HEX_LEN, "group_id")
    if not charter.admins:
        raise MalformedRecordError("charter must declare at least one admin")
    try:
        idkit_verify_signature(
            charter.author, charter.signature, charter.signing_input()
        )
    except (IdkitError, ValueError) as exc:
        raise RecordSignatureError("charter signature does not verify") from exc


def accept_charter(fold, charter: GroupCharter) -> None:
    """Version-one chartering authority: a current organization member.

    The delegated group-creation capability (an org-root cert making
    every charter auditable) is the tracked tightening; until it lands,
    a charter from outside the organization is refused here.
    """
    verify_charter(charter)
    if charter.author not in domain_member_keys(fold):
        raise RecordSignatureError("charter author is not a current organization member")


def _event_valid(charter: GroupCharter, event: GroupEvent) -> bool:
    """The admission predicate: is this record a group event at all?

    A failing record is not an error to surface — it is simply not a
    valid group event (design Decision 3) and replay skips it. Malformed
    shapes, wrong-group or wrong-charter records, non-admin authors, and
    bad signatures all land here.
    """
    try:
        if event.version != GROUP_VERSION or event.op not in _OPS:
            return False
        if event.group_id != charter.group_id:
            return False
        if event.charter_id != charter.charter_id:
            return False
        if event.author not in charter.admins:
            return False
        _require_hex(event.subject, _KEY_HEX_LEN, "subject")
        _require_hlc(event.hlc, "hlc")
        idkit_verify_signature(
            event.author, event.signature, event.signing_input()
        )
        return True
    except (IdkitError, MalformedRecordError, ValueError):
        return False


def replay_group(charter: GroupCharter, events) -> frozenset:
    """The group's own half of the fold: referenced personas, pre-intersection.

    Deterministic regardless of arrival order: valid events replay
    sorted by (hlc, op rank, event id), last operation per subject wins,
    and a remove outranks an add at an identical HLC (safety bias).
    """
    verify_charter(charter)
    members = set(charter.initial_members)
    valid = [e for e in events if _event_valid(charter, e)]
    valid.sort(key=lambda e: (tuple(e.hlc), _OP_RANK[e.op], e.event_id))
    for event in valid:
        if event.op == "add":
            members.add(event.subject)
        else:
            members.discard(event.subject)
    return frozenset(members)


def group_member_keys(fold, charter: GroupCharter, events) -> frozenset:
    """Effective membership: the derived fold's defining intersection."""
    return replay_group(charter, events) & domain_member_keys(fold)


def group_member_credentials(fold, charter, events, key_control, authority_ancestry) -> tuple:
    """The grant recipients a GROUP mint seals to — the single seam.

    Mirrors ``_current_member_credentials`` exactly: every effective
    member resolved to its ONE current credential; a persona removed
    from the group or the organization is absent from the intersection
    and receives nothing; a member without a published credential is
    skipped rather than invented.
    """
    recipients = []
    for persona in sorted(group_member_keys(fold, charter, events)):
        candidates = key_control.credentials_for_persona(persona)
        if not candidates:
            continue
        recipients.append(select_current_credential(candidates, authority_ancestry))
    return tuple(recipients)


def group_content_domain_id(genesis_id: str, group_id: str) -> str:
    """The version-one group-content storage-domain identifier.

    ``SHA-256("autonomy/storage-domain/v1" || genesis_id ||
    "group-content" || group_id)`` — mirroring the organization content
    domain's derivation and anchored the same way: on the genesis event
    id (invariant across a ``key.rotate``) plus the stable group id
    (invariant across membership churn, per the ratified rule that an
    audience identifier never derives from its member list).
    """
    import hashlib

    _require_hex(genesis_id, _ID_HEX_LEN, "genesis_id")
    _require_hex(group_id, _ID_HEX_LEN, "group_id")
    return hashlib.sha256(
        b"autonomy/storage-domain/v1"
        + genesis_id.encode("ascii")
        + b"group-content"
        + group_id.encode("ascii")
    ).hexdigest()


def group_head_grants(
    grantor,
    charter: GroupCharter,
    events,
    fold,
    credentials_by_persona,
    head_descriptor,
    head_secret,
    frontier,
    existing_grants=(),
) -> tuple:
    """One grant per (effective group member x this head): the group mint.

    The group twin of ``distribution.provision_missing``: identical grant
    primitive, identical dedup against already-minted grants, identical
    skip for a member without a published credential — only the recipient
    set differs, computed by the derived fold instead of the whole-domain
    roster. Removal (from the group or from the organization) excludes a
    persona from the next fan-out with no other machinery: minting a
    fresh state and fanning out to the survivors IS the re-mint.
    """
    from .distribution import grant_current_head

    provisioned = {
        (grant.storage_state_id, grant.recipient_kem_key_id)
        for grant in existing_grants
    }
    minted = []
    for persona in sorted(group_member_keys(fold, charter, events)):
        credential = credentials_by_persona.get(persona)
        if credential is None:
            continue
        if (head_descriptor.state_id, credential.kem_key_id) in provisioned:
            continue
        minted.append(
            grant_current_head(
                grantor,
                head_descriptor.domain_id,
                credential,
                head_descriptor,
                head_secret,
                frontier,
            )
        )
    return tuple(minted)
