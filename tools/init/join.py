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
   its password is never taken from an environment value (it would persist
   in ``docker inspect`` for the node's life). Production accepts only a
   one-time stdin value. The mounted-file source exists solely for the
   isolated multi-node test harness and is guarded by explicit test mode
   plus refusal of every real-data fallback. With no password source, the
   node stages the validated invitation and mints NOTHING, leaving identity
   setup to an interactive browser ceremony.
3. **Claim over the channel**, reusing the membership flow unchanged:
   derive the persona from the org's real ``genesis_id``, mint the
   credential-carrying claim, submit. A token-bound invite stages
   pending by design (bearer safety) — that is a successful outcome
   here, not a failure, and resuming it is B4b's persistence.
"""

from __future__ import annotations

import asyncio
import json
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
    #: The org's genesis event id — the input this persona was derived under.
    #: Carried on the outcome because persist_outcome records the persona and
    #: the genesis IS its identifier; without it the record cannot be written.
    genesis_id: Optional[str] = None
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


# -- the password source ------------------------------------------------------------

# TEST AUTOMATION ONLY. The production name previously used for a mounted
# plaintext personal passphrase is rejected below rather than retained as an
# undocumented compatibility alias. A personal passphrase unlocks every
# persona this identity derives, so making a persistent server-side copy must
# never become a production convenience by accident.
TEST_AUTOMATION_ENV = "AUTONOMY_TEST_AUTOMATION"
TEST_PASSWORD_FILE_ENV = "AUTONOMY_TEST_PERSONAL_PASSWORD_FILE"
LEGACY_PASSWORD_FILE_ENV = "AUTONOMY_PERSONAL_PASSWORD_FILE"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def test_automation_enabled() -> bool:
    """Whether isolated test-only secret inputs are explicitly enabled."""
    return os.environ.get(TEST_AUTOMATION_ENV, "").strip().lower() in _TRUE_VALUES


def read_personal_password(*, stdin_ok: bool = True) -> Optional[str]:
    """The first-run personal-root password, or None to stage instead.

    Production may supply one line over stdin. A mounted file is accepted
    ONLY by isolated test automation with both ``AUTONOMY_TEST_AUTOMATION``
    and ``AUTONOMY_REFUSE_REAL_DATA_FALLBACK`` enabled. The latter ensures
    the same process cannot resolve any operator data through a default path.

    The password itself is NEVER an environment value: unlike the invitation's
    channel and claim credentials, it unlocks every persona in every org this
    identity will ever join, and an environment value would remain visible in
    ``docker inspect`` for the node's whole life.
    """
    if LEGACY_PASSWORD_FILE_ENV in os.environ:
        raise JoinError(
            f"{LEGACY_PASSWORD_FILE_ENV} is not a supported production input; "
            "mounted personal-passphrase files are test automation only"
        )

    path = os.environ.get(TEST_PASSWORD_FILE_ENV)
    if path:
        from tools.data_paths import refuse_real_data_fallback_enabled

        if not test_automation_enabled() or not refuse_real_data_fallback_enabled():
            raise JoinError(
                f"{TEST_PASSWORD_FILE_ENV} is TEST AUTOMATION ONLY and requires "
                f"{TEST_AUTOMATION_ENV}=1 plus "
                "AUTONOMY_REFUSE_REAL_DATA_FALLBACK=1"
            )
        try:
            value = Path(path).read_text().splitlines()[0].strip()
        except (OSError, IndexError) as exc:
            raise JoinError(f"cannot read {TEST_PASSWORD_FILE_ENV}: {exc}") from exc
        if not value:
            raise JoinError(f"{TEST_PASSWORD_FILE_ENV} is empty")
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


def personal_identity_exists() -> bool:
    from tools.graph import settings_ops
    from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID

    return any(
        isinstance(member.payload, dict)
        and bool(member.payload.get("armored_private_key"))
        for member in settings_ops.read_owned_set(
            PERSONAL_IDENTITY_SET_ID, org=None
        ).members
    )


def _open_personal_identity(password: str) -> bytes:
    """Decrypt the canonical local personal identity, never mint a new one."""
    from tools.graph import settings_ops
    from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
    from tools.network.idkit.armor import ArmorError, decrypt_root_key

    members = [
        member
        for member in settings_ops.read_owned_set(
            PERSONAL_IDENTITY_SET_ID, org=None
        ).members
        if isinstance(member.payload, dict)
        and member.payload.get("armored_private_key")
    ]
    member = next(
        (candidate for candidate in members if candidate.key == "default"),
        members[0] if members else None,
    )
    if member is None:
        raise JoinError("no personal identity is enrolled on this node")
    try:
        root = decrypt_root_key(member.payload["armored_private_key"], password)
    except ArmorError:
        raise JoinError("the supplied personal password did not unlock the identity") from None
    return bytes.fromhex(root.private_hex)


def _outcome_from_submit(
    invitation: Invitation,
    persona_pub: str,
    role: str,
    reply: dict,
    genesis_id: Optional[str] = None,
) -> JoinOutcome:
    status = reply.get("status")
    if status == "admitted":
        return JoinOutcome(
            state=ADMITTED, org=invitation.org, invite_ref=invitation.invite_ref,
            persona_pub=persona_pub, genesis_id=genesis_id, granted_role=role,
            detail=f"admitted to {invitation.org} as {role}",
        )
    if status == "pending":
        return JoinOutcome(
            state=PENDING, org=invitation.org, invite_ref=invitation.invite_ref,
            persona_pub=persona_pub, genesis_id=genesis_id, granted_role=role,
            have=reply.get("have"), need=reply.get("need"),
            detail=f"awaiting approval ({reply.get('have')} of {reply.get('need')})",
        )
    raise JoinError(f"claim rejected: {reply.get('reason', status)!r}")


def _initial_claim(
    invitation: Invitation,
    transport: JoinTransport,
    context: dict,
    seed: bytes,
    *,
    kem_seed: Optional[bytes] = None,
) -> JoinOutcome:
    from tools.network.idkit import derive_persona
    from tools.network.ledger import HLC
    from tools.network.ledger.claims import mint_member_claim

    genesis_id = context["genesis_id"]
    role = context["granted_role"]
    persona = derive_persona(seed, genesis_id)
    credential = None
    if kem_seed is not None:
        from tools.network.storagekit import credentials as credentials_mod

        record, _private = credentials_mod.build(
            persona, genesis_id, kem_seed, list(context["heads"]),
            tuple(context["max_hlc"]),
        )
        credential = record.to_dict()
    ts, _count = context["max_hlc"]
    event, _ = mint_member_claim(
        seed, genesis_id,
        invite_ref=invitation.invite_ref,
        heads=list(context["heads"]),
        # Tick past the org's newest head: a lagging node clock would
        # otherwise mint an event the ledger refuses on causality.
        hlc=HLC(int(ts) + 1_000, 0),
        token=invitation.claim_token,
        kem_credential=credential,
    )
    reply = transport.request(
        {"v": 1, "op": "submit", "event": event.to_json().decode("utf-8")}
    )
    return _outcome_from_submit(
        invitation, persona.public_hex, role, reply, genesis_id=genesis_id
    )


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
    context = _context(transport, invitation)
    verify_anchor(context, invitation)
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
    return _initial_claim(
        invitation, transport, context, seed, kem_seed=kem_seed
    )


def join_existing_identity(
    invitation: Invitation,
    transport: JoinTransport,
    *,
    password: str,
) -> JoinOutcome:
    """Start or resume a join with the identity already enrolled locally."""
    from tools.dashboard.dao import pending_joins
    from tools.network.idkit import derive_persona

    local_pending = pending_joins.get(invitation.invite_ref)
    if local_pending is not None:
        # Resume is deliberately status-first. context() remains a pure,
        # TTL-gated fresh-join bootstrap and correctly returns gone after the
        # invitation expires; a legitimately staged claim must still finalize
        # from its server-recorded causal position after that wall-clock edge.
        if local_pending["org"] != invitation.org:
            raise JoinError("local pending join does not match the invitation org")
        expected_key = pending_joins.claim_key(
            invitation.invite_ref, local_pending["persona_pub"]
        )
        if local_pending["claim_key"] != expected_key:
            raise JoinError("local pending join locator is inconsistent")
        seed = _open_personal_identity(password)
        persona_pub = local_pending["persona_pub"]
        reply = transport.request({
            "v": 1,
            "op": "status",
            "persona_pub": persona_pub,
        })
        if reply.get("status") == "admitted":
            return JoinOutcome(
                state=ADMITTED,
                org=invitation.org,
                invite_ref=invitation.invite_ref,
                persona_pub=persona_pub,
                detail=f"already admitted to {invitation.org}",
            )
        if reply.get("status") == "absent":
            raise JoinError("the local pending join is absent from the org")
        if reply.get("status") == "rejected":
            raise JoinError(
                f"pending claim is terminal: {reply.get('reason')!r}"
            )
        if reply.get("status") != "pending":
            raise JoinError(f"join status response is malformed: {reply.get('status')!r}")
        genesis_id = reply.get("genesis_id")
        role = reply.get("granted_role")
        if not isinstance(genesis_id, str) or not isinstance(role, str):
            raise JoinError("pending join status omitted its resume identity")
        if derive_persona(seed, genesis_id).public_hex != persona_pub:
            raise JoinError(
                "personal identity does not match the staged join persona"
            )
        return _resume_pending(
            invitation,
            transport,
            seed=seed,
            genesis_id=genesis_id,
            persona_pub=persona_pub,
            role=role,
            reply=reply,
        )

    # No durable local proof of an earlier staging: this is a fresh join and
    # must pass context()'s live-invitation check before doing anything else.
    context = _context(transport, invitation)
    verify_anchor(context, invitation)
    seed = _open_personal_identity(password)
    persona = derive_persona(seed, context["genesis_id"])
    role = context["granted_role"]
    reply = transport.request({
        "v": 1,
        "op": "status",
        "persona_pub": persona.public_hex,
    })
    status = reply.get("status")
    if status == "admitted":
        return _outcome_from_submit(
            invitation, persona.public_hex, role, reply,
            genesis_id=context["genesis_id"],
        )
    if status == "absent":
        return _initial_claim(invitation, transport, context, seed)
    if status == "rejected":
        raise JoinError(f"pending claim is terminal: {reply.get('reason')!r}")
    if status != "pending":
        raise JoinError(f"join status response is malformed: {status!r}")
    if (
        reply.get("genesis_id") != context["genesis_id"]
        or reply.get("granted_role") != role
    ):
        raise JoinError("pending join status does not match its live context")
    return _resume_pending(
        invitation,
        transport,
        seed=seed,
        genesis_id=context["genesis_id"],
        persona_pub=persona.public_hex,
        role=role,
        reply=reply,
    )


def _resume_pending(
    invitation: Invitation,
    transport: JoinTransport,
    *,
    seed: bytes,
    genesis_id: str,
    persona_pub: str,
    role: str,
    reply: dict,
) -> JoinOutcome:
    """Return pending or finalize from the service's authoritative subset."""
    from tools.network.ledger import HLC
    from tools.network.ledger.claims import mint_member_claim

    have, need = reply.get("have"), reply.get("need")
    if (
        not isinstance(have, int)
        or isinstance(have, bool)
        or not isinstance(need, int)
        or isinstance(need, bool)
        or have < 0
        or need < 1
        or have > need
    ):
        raise JoinError("join status response has invalid approval counts")
    if have < need:
        return JoinOutcome(
            state=PENDING, org=invitation.org, invite_ref=invitation.invite_ref,
            persona_pub=persona_pub, genesis_id=genesis_id, granted_role=role,
            have=have, need=need,
            detail=f"awaiting approval ({have} of {need})",
        )

    approvals = reply.get("approvals")
    admitting = reply.get("admitting")
    position = reply.get("position")
    if (
        not isinstance(approvals, list)
        or not isinstance(admitting, list)
        or not isinstance(position, dict)
        or set(position) != {"parents", "hlc"}
    ):
        raise JoinError("ready claim status omitted approvals or stored position")
    by_key = {
        entry.get("key"): entry
        for entry in approvals
        if isinstance(entry, dict) and set(entry) == {"key", "sig"}
    }
    try:
        selected = [by_key[key] for key in admitting]
    except (KeyError, TypeError):
        raise JoinError("ready claim status has an invalid admitting subset") from None
    if len(selected) != need or len(set(admitting)) != len(admitting):
        raise JoinError("ready claim status has a non-authoritative admitting subset")
    try:
        final, _ = mint_member_claim(
            seed,
            genesis_id,
            invite_ref=invitation.invite_ref,
            heads=position["parents"],
            hlc=HLC(*position["hlc"]),
            token=invitation.claim_token,
            approvals=selected,
        )
    except (KeyError, TypeError, ValueError):
        raise JoinError("ready claim status has a malformed stored position") from None
    admitted = transport.request({
        "v": 1,
        "op": "submit",
        "event": final.to_json().decode("utf-8"),
    })
    return _outcome_from_submit(
        invitation, persona_pub, role, admitted, genesis_id=genesis_id
    )


def persist_outcome(outcome: JoinOutcome) -> None:
    """Persist only restart-safe public progress, or clear after admission."""
    from tools.dashboard.dao import pending_joins

    if outcome.state == PENDING:
        pending_joins.save(
            org=outcome.org,
            invite_ref=outcome.invite_ref,
            persona_pub=outcome.persona_pub,
            have=outcome.have,
            need=outcome.need,
        )
    elif outcome.state == ADMITTED:
        # Admission is the moment the persona becomes true, and it is also the
        # moment the pending row carrying it is deleted below — so record it
        # first. Until this ran, the node derived its persona three times and
        # kept it nowhere, leaving "which member am I?" answerable only by
        # unsealing the personal seed and re-deriving.
        if outcome.persona_pub and outcome.genesis_id:
            from tools.graph.org_ops import _record_persona_setting

            _record_persona_setting(
                outcome.org, outcome.genesis_id, outcome.persona_pub,
                source="join", invite_ref=outcome.invite_ref,
            )
        # Never clear merely because a status/finalize attempt ran. The org's
        # admitted verdict means the invitee-signed event actually appended.
        pending_joins.delete(outcome.invite_ref)


class ViewerJoinTransport:
    """Synchronous adapter over one-request E2E ViewerChannels."""

    def __init__(
        self,
        invitation: Invitation,
        *,
        relay_url: str,
        timeout: float = 10.0,
    ):
        self._invitation = invitation
        self._relay_url = relay_url.rstrip("/")
        self._timeout = timeout

    def __repr__(self) -> str:
        return (
            f"ViewerJoinTransport(org={self._invitation.org!r}, "
            f"relay_url={self._relay_url!r}, channel_token=<redacted>, "
            "claim_token=<redacted>)"
        )

    async def _request(self, payload: dict) -> dict:
        from tools.network.idkit import canonical_json
        from tools.network.relaykit.viewer import ViewerChannel

        channel = await ViewerChannel.connect(
            self._relay_url,
            self._invitation.channel_token,
            root_pub=self._invitation.root_pub,
            org=self._invitation.org,
            open_timeout=self._timeout,
        )
        async with channel:
            await channel.send_message(canonical_json(payload))
            raw = await channel.recv_message()
        reply = json.loads(raw)
        if not isinstance(reply, dict) or reply.get("v") != 1:
            raise JoinError("join channel returned a malformed response")
        return reply

    def request(self, payload: dict) -> dict:
        try:
            return asyncio.run(
                asyncio.wait_for(self._request(payload), timeout=self._timeout)
            )
        except JoinError:
            raise
        except Exception:
            # The underlying websocket exception may include the token-bearing
            # URL. Suppress the chain so logs never recover the bearer.
            raise JoinError("the encrypted join channel is unavailable") from None


def production_transport(invitation: Invitation) -> ViewerJoinTransport:
    from tools.dashboard.link_probe import registry_to_relay_ws
    from tools.dashboard.network_routes import DEFAULT_REGISTRY_URL

    registry = (
        os.environ.get("AUTONOMY_NETWORK_REGISTRY_URL")
        or DEFAULT_REGISTRY_URL
    )
    return ViewerJoinTransport(
        invitation,
        relay_url=registry_to_relay_ws(registry),
    )
