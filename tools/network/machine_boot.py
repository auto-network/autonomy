"""First boot on a fleet link: mint the machine id and ask to join (auto-b6fee).

The corrected, thin first-boot flow. The machine mints its OWN random machine
id, stores that id (only) in machine.db, and presents it over the relaykit
channel the invite link establishes. It does NOT generate or store a key: the
machine's operating key is DERIVED from ``personal_root + machine_id``
(``idkit.derive_machine_key``) on demand, once the root has been provisioned
after approval (auto-5ydhe). So there is no machine keypair, no machine-local
factor, and no vault sealing here — the id is public and the key regenerates.

The join request is the id over the already-authenticated tunnel — no key
proof of possession, because there is no key yet; the tunnel plus the
short-authentication-string (the id fingerprint the operator compares on both
screens) authenticate the enrolment. The proof-of-possession primitive
(``fleet_enroll.build_request``) is for CHANNEL AUTH later, once the machine
holds its derived key on a live fleet connection — not for this request.

Idempotent: a second boot with an id already present reuses it, never mints a
second. Declining leaves nothing published — the id is inert local state until
the primary writes ``{id, derived_pub}`` into the roster — so cleanup is a
local delete.
"""

from __future__ import annotations

from dataclasses import dataclass

from tools.network.idkit import KeyPair, derive_machine_key, mint_machine_id
from tools.network.fleet_invite import FleetInvite
from tools.network import fleet_enroll


class MachineBootError(RuntimeError):
    """The machine identity could not be created, read, or discarded."""


@dataclass(frozen=True)
class JoinRequest:
    """A machine's ask-to-join, carried over the relaykit channel. All public:
    the machine's id and the invite it is answering. No key, no signature —
    the authenticated tunnel and the operator's id-fingerprint comparison are
    what authenticate it."""

    machine_id: str
    invite_id: str
    personal_root_pub: str  # the fleet anchor, from the invite


def has_identity(*, org="machine") -> bool:
    """Whether this machine already minted its id — the idempotency gate that
    makes a second boot reuse the id rather than mint a new one."""
    return machine_id(org=org) is not None


def machine_id(*, org="machine") -> str | None:
    row = _read_row(org=org)
    return row["machine_id"] if row is not None else None


def first_boot(invite: FleetInvite, *, org="machine") -> tuple[JoinRequest, str]:
    """Mint this machine's id, store it, and return the join request plus the
    id fingerprint the operator compares at approval. Refuses if an id already
    exists (a second boot reuses it — the caller checks :func:`has_identity`)."""
    if has_identity(org=org):
        raise MachineBootError(
            "this machine already has an id; reuse it, do not mint a new one"
        )
    mid = mint_machine_id()
    _write_row({"machine_id": mid}, org=org)
    return build_join_request(mid, invite), fleet_enroll.fingerprint(mid)


def build_join_request(machine_id: str, invite: FleetInvite) -> JoinRequest:
    """The join request: this machine's id answering this invite. Presented
    over the relaykit tunnel; carries no key (there is none yet)."""
    return JoinRequest(
        machine_id=machine_id,
        invite_id=invite.invite_id,
        personal_root_pub=invite.personal_root_pub,
    )


def operating_key(personal_root_seed: bytes, *, org="machine") -> KeyPair:
    """Derive this machine's operating key from the provisioned root and this
    machine's id. Available only AFTER the root has been provisioned
    (auto-5ydhe); the key is re-derived every time and never stored or sealed —
    it is safe because the root is (under the one boot factor) and the id is
    public. The primary derived the same key's PUBLIC half at approval and
    wrote it to the roster."""
    mid = machine_id(org=org)
    if mid is None:
        raise MachineBootError("this machine has no id; run first boot first")
    return derive_machine_key(personal_root_seed, mid)


def discard(*, org="machine") -> bool:
    """No-orphan cleanup for a declined join: hard-delete the machine-local id
    row, so a fresh first boot mints a new id. Nothing was published — the id
    authorized nothing until the primary wrote it to the roster — so a direct
    delete on the machine's own store is correct. Returns True if removed."""
    from pathlib import Path

    from tools.graph.db import GraphDB, _org_db_path

    removed = False
    path = _org_db_path("machine")
    if Path(path).exists():
        db = GraphDB(path, mode="rw")
        try:
            cur = db.conn.execute(
                "DELETE FROM settings WHERE set_id = ? AND key = ?",
                (_SET_ID, _KEY),
            )
            db.conn.commit()
            removed = cur.rowcount > 0
        finally:
            db.close()
    return removed


_SET_ID = "autonomy.machine.identity"
_KEY = "self"


def _write_row(payload: dict, *, org) -> None:
    from tools.graph import settings_ops
    from tools.graph.schemas.machine_identity import MACHINE_IDENTITY_REVISION

    settings_ops.add_setting(
        _SET_ID, MACHINE_IDENTITY_REVISION, _KEY, payload, org=org, state="raw",
    )


def _read_row(*, org) -> dict | None:
    from tools.graph import settings_ops
    from tools.graph.schemas.machine_identity import MACHINE_IDENTITY_REVISION

    members = settings_ops.read_owned_set(
        _SET_ID, org=org, target_revision=MACHINE_IDENTITY_REVISION,
    ).to_dict()
    member = members.get(_KEY)
    return member.payload if member is not None else None
