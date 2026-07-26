"""Invitation-aware join orchestration (auto-8v5ri part 3).

A node handed ``AUTONOMY_INVITE`` on a fresh volume joins an existing
org instead of founding one. The whole ceremony runs in the node the
operator already validated — nothing here is served by the relay, which
carries only ciphertext frames between the joining node and the org's.

The order matters and is enforced:

1. **Pin from the invitation, not the transport.** ``root_pub`` arrives
   in the user-carried code; the channel is opened against it, and the
   context the org returns is checked against the invitation's anchor.
   A relay that answers for the wrong org fails the check rather than
   silently joining the invitee somewhere else.
2. **Mint locally, or not at all.** The personal root is the asset that
   unlocks every persona in every org this identity will ever join, so
   its password is never taken from the environment (it would persist
   in ``docker inspect`` for the node's life). It comes from a mounted
   secret file or stdin, read ONCE — or, with no password source, the
   node stages the validated invitation and mints NOTHING, leaving
   identity setup to the dashboard's local prompt.
3. **Claim over the channel**, reusing the membership flow unchanged:
   derive the persona from the org's real ``genesis_id``, mint the
   credential-carrying claim, submit. A token-bound invite stages
   pending by design (bearer safety) — that is a successful outcome
   here, not a failure, and resuming it is B4b's persistence.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from tools.network.invitation import Invitation

#: Outcomes of a join attempt.
ADMITTED = "admitted"
PENDING = "pending"
STAGED = "staged"  # validated, awaiting an identity the operator must create


class JoinError(RuntimeError):
    """The join could not proceed. Never carries the bearer or a password."""


class AnchorMismatch(JoinError):
    """The org on the channel is not the org the invitation names."""


@dataclass(frozen=True)
class JoinOutcome:
    state: str
    org: str
    invite_ref: str
    persona_pub: Optional[str] = None
    granted_role: Optional[str] = None
    have: Optional[int] = None
    need: Optional[int] = None
    detail: str = ""


class JoinTransport(Protocol):
    """One request/response over the org:join channel.

    Production is relaykit's ``ViewerChannel`` (E2E, pinned to the org
    root). Tests supply an in-process transport so the orchestration
    under test is the real one.
    """

    def request(self, payload: dict) -> dict: ...


# -- the password source (ruling: file/stdin only, never the environment) -----------

PASSWORD_FILE_ENV = "AUTONOMY_PERSONAL_PASSWORD_FILE"


def read_personal_password(*, stdin_ok: bool = True) -> Optional[str]:
    """The first-run personal-root password, or None to stage instead.

    A mounted secret file, else stdin when it is not a terminal. NEVER
    the environment: unlike the invite bearer (one token, host access
    already implies node control), this unlocks every persona in every
    org this identity will ever join, and an environment variable would
    keep it in ``docker inspect`` for the node's whole life.
    """
    path = os.environ.get(PASSWORD_FILE_ENV)
    if path:
        try:
            value = Path(path).read_text().splitlines()[0].strip()
        except (OSError, IndexError) as exc:
            raise JoinError(f"cannot read {PASSWORD_FILE_ENV}: {exc}") from exc
        if not value:
            raise JoinError(f"{PASSWORD_FILE_ENV} is empty")
        return value
    if stdin_ok:
        try:
            if not sys.stdin.isatty():
                value = sys.stdin.readline().strip()
                if value:
                    return value
        except (OSError, ValueError):
            # Not-a-tty does NOT imply readable: `docker run` without -i,
            # systemd, and cron all present a stdin that raises on read.
            # No password source is a valid state — it means stage (c),
            # so this must not take first-run down.
            return None
    return None


# -- the ceremony -------------------------------------------------------------------


def _context(transport: JoinTransport, invitation: Invitation) -> dict:
    reply = transport.request({"v": 1, "op": "context"})
    if not isinstance(reply, dict):
        raise JoinError("join channel returned a malformed context")
    status = reply.get("status")
    if status == "gone":
        raise JoinError(f"invitation is no longer usable: {reply.get('reason')}")
    if status != "ok":
        raise JoinError(f"join channel refused the context request: {status!r}")
    for required in ("genesis_id", "heads", "max_hlc", "granted_role", "binding"):
        if required not in reply:
            raise JoinError(f"join context is missing {required!r}")
    return reply


def verify_anchor(context: dict, invitation: Invitation) -> None:
    """The org that answered must be the org the invitation names.

    The channel is already pinned to ``invitation.root_pub`` by the
    handshake; this re-checks the identity the org states about itself,
    so a transport that somehow reached a different node cannot pass a
    context off as the invited org's.
    """
    stated = context.get("org")
    if stated is not None and stated != invitation.org:
        raise AnchorMismatch(
            f"channel answered for org {stated}, invitation names {invitation.org}"
        )
    stated_root = context.get("root_pub")
    if stated_root is not None and stated_root != invitation.root_pub:
        raise AnchorMismatch("channel presented a different org root than the invitation")


def _mint_personal_identity(password: str, *, display_name: str) -> bytes:
    """Create the node operator's personal identity locally; return its seed."""
    from tools.graph import settings_ops
    from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import encrypt_root_key

    root = KeyPair.generate()
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID, 1, "default",
            {
                "armored_private_key": encrypt_root_key(root, password),
                "root_pub": root.public_hex,
                "display_name": display_name,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            org=None,
        )
    return bytes.fromhex(root.private_hex)


def join_org(
    invitation: Invitation,
    transport: JoinTransport,
    *,
    password: Optional[str] = None,
    display_name: str = "Node operator",
    kem_seed: Optional[bytes] = None,
) -> JoinOutcome:
    """Run the join ceremony; returns admitted / pending / staged.

    ``password`` absent means the operator has supplied no first-run
    secret: the invitation is validated against the live org and staged,
    and no identity is minted (ruling (c)).
    """
    from tools.network.idkit import derive_persona
    from tools.network.ledger import HLC
    from tools.network.ledger.claims import mint_member_claim

    context = _context(transport, invitation)
    verify_anchor(context, invitation)
    genesis_id = context["genesis_id"]
    role = context["granted_role"]

    if password is None:
        return JoinOutcome(
            state=STAGED, org=invitation.org, invite_ref=invitation.invite_ref,
            granted_role=role,
            detail="invitation verified against the live org; no password source "
                   "at first run, so no identity was minted — complete identity "
                   "setup in the dashboard and the join resumes",
        )

    seed = _mint_personal_identity(password, display_name=display_name)
    persona = derive_persona(seed, genesis_id)

    credential = None
    if kem_seed is not None:
        from tools.network.storagekit import credentials as credentials_mod

        record, _private = credentials_mod.build(
            persona, genesis_id, kem_seed, list(context["heads"]),
            tuple(context["max_hlc"]),
        )
        credential = record.to_dict()

    ts, count = context["max_hlc"]
    event, _ = mint_member_claim(
        seed, genesis_id,
        invite_ref=invitation.invite_ref,
        heads=list(context["heads"]),
        # Tick past the org's newest head: a lagging node clock would
        # otherwise mint an event the ledger refuses on causality.
        hlc=HLC(int(ts) + 1_000, 0),
        token=invitation.token,
        kem_credential=credential,
    )
    reply = transport.request(
        {"v": 1, "op": "submit", "event": event.to_json().decode("utf-8")}
    )
    status = reply.get("status")
    if status == "admitted":
        return JoinOutcome(
            state=ADMITTED, org=invitation.org, invite_ref=invitation.invite_ref,
            persona_pub=persona.public_hex, granted_role=role,
            detail=f"admitted to {invitation.org} as {role}",
        )
    if status == "pending":
        return JoinOutcome(
            state=PENDING, org=invitation.org, invite_ref=invitation.invite_ref,
            persona_pub=persona.public_hex, granted_role=role,
            have=reply.get("have"), need=reply.get("need"),
            detail=f"awaiting approval ({reply.get('have')} of {reply.get('need')})",
        )
    raise JoinError(f"claim rejected: {reply.get('reason', status)!r}")
