"""The deterministic fold — the ledger's security policy.

The fold turns a DAG of signed events into an authority state. Its output
is a pure function of the event set: replicas holding the same events
compute identical state no matter what order events arrived in (L1).

Three layers, from the spec (§3):

**1. Issuance validity (per event, causal-ancestry only).** An event is
valid iff its author held the required authority *in the state derived
from the event's own causal ancestry*. An overreaching delegate/invite
(L2), an unauthorized revoke, a claim against a dead invite — all are
marked invalid and inert; they stay in the DAG (replicas must exchange
them to converge) but never have effects.

**2. Revocation (safety-biased, L3).** A revocation kills targets that are
*causally ancestral or concurrent* to it — so a revoke racing a grant
always wins, in every replay order — while an explicit re-grant issued
causally *after* the revoke survives (L4 "re-grants are explicit").
Additionally, a grant whose author's supporting authority was broken by a
*concurrent* revocation is permanently void: it does not spring back to
life if its author is later re-granted. Equal-rank conflicts (two
concurrent claims of one invite, concurrent role.define at one version,
concurrent rotations) break deterministically by event hash.

**3. Liveness / cascade (L4).** Standing authority (delegations, unclaimed
invites) is live only while a chain of live grants connects it to the root
lineage — revoking a delegation silently de-authorizes every descendant
that lacks another live path. Facts (memberships, role grants) persist
once established: they die only by explicit revocation or by losing a
race, never because their granter was later demoted (the sponsor trail is
provenance, not a liveness dependency).

Roles carry scopes: holders of a role hold its ``scope_set`` (non-
delegable) via their current persona key. To keep that attenuation-only,
``role.define`` requires the definer's *delegable* authority to cover the
role's scope set (L2 applies to role definitions like any other hop).
"""

from __future__ import annotations

import hashlib
import heapq
import sys
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

from tools.network.idkit import canonical_json, verify_signature
from tools.network.idkit.errors import IdkitError

from .events import (
    KEM_CREDENTIAL_DOMAIN,
    approval_signing_input,
    rotate_continuity_input,
)
from .ledger import Ledger
from .scopes import UNIVERSE, attenuates, covered_subset, set_covers

# Stable invalidity reasons (pinned by tests).
R_SCOPE_ESCALATION = "scope-escalation"
R_NOT_REDELEGABLE = "not-redelegable"
R_REVOKE_UNAUTHORIZED = "revoke-unauthorized"
R_REVOKE_BAD_TARGET = "revoke-bad-target"
R_REVOKE_NOT_IN_ANCESTRY = "revoke-target-not-in-ancestry"
R_ROLE_DEFINE_UNAUTHORIZED = "role-define-unauthorized"
R_ROLE_DEFINE_OVERREACH = "role-define-overreach"
R_ROLE_UNDEFINED = "role-undefined"
R_ROLE_GRANT_UNAUTHORIZED = "role-grant-unauthorized"
R_ROLE_REVOKE_UNAUTHORIZED = "role-revoke-unauthorized"
R_INVITE_OVERREACH = "invite-overreach"
R_INVITE_SPONSOR_MISMATCH = "invite-sponsor-mismatch"
R_INVITE_NOT_IN_ANCESTRY = "invite-not-in-ancestry"
R_INVITE_DEAD = "invite-dead"
R_INVITE_EXPIRED = "invite-expired"
R_INVITE_ALREADY_CLAIMED = "invite-already-claimed"
R_CLAIM_WRONG_KEY = "claim-wrong-key"
R_CLAIM_BAD_TOKEN = "claim-bad-token"
R_CLAIM_BAD_CREDENTIAL = "claim-bad-credential"
R_PERSONA_EXISTS = "persona-exists"
R_APPROVAL_BAD = "bad-approval"
R_APPROVAL_MISSING = "approval-missing"
R_UNKNOWN_PERSONA = "unknown-persona"
R_REKEY_WRONG_KEY = "rekey-wrong-key"
R_REKEY_UNAUTHORIZED = "rekey-unauthorized"
R_REKEY_REVOKED_KEY = "rekey-revoked-key"
R_NOT_ROOT = "not-root"
R_ROTATE_WRONG_OLD = "rotate-wrong-old"
R_BAD_CONTINUITY = "bad-continuity"
R_CHECKPOINT_UNAUTHORIZED = "checkpoint-unauthorized"

INVITE_LIVE = "live"
INVITE_CLAIMED = "claimed"
INVITE_REVOKED = "revoked"
INVITE_DEAD = "dead"
INVITE_EXPIRED = "expired"

SCOPE_ROLE_DEFINE = "role:define"
SCOPE_CHECKPOINT = "checkpoint"


def scope_role_grant(role: str) -> str:
    return f"role:grant:{role}"


def admitting_approvers(approver_keys, *, root, sponsor, role, holds) -> frozenset:
    """The distinct approvers that count toward admitting a claim: root,
    the invite's sponsor (vouch counts as one — D16), or a holder of
    ``role:grant:<role>`` per the *holds(key, scope)* callable."""
    return frozenset(
        key
        for key in approver_keys
        if key == root or key == sponsor or holds(key, scope_role_grant(role))
    )


def claim_requirement_status(
    *, requires, key_bound, approver_keys, threshold, root, sponsor, role, holds
) -> tuple:
    """``(have, need)`` under the claim-acceptance rule; admitted when
    ``have >= need``.

    THE single source of the acceptance core (B6 bearer safety, sponsor,
    admin-ack, D16 threshold counting): ``_check_approvals`` applies it
    at fold time and ``LedgerStore.evaluate_pending_claim`` applies it
    to a staged body with merged approvals, so route-side readiness can
    never drift from the fold's verdict. ``need`` is the TOTAL required
    count (the "N of M" view); a key-bound ``self`` claim needs zero.
    """
    if requires == "self" and key_bound:
        return (0, 0)  # a key binding establishes the redeemer (B6)
    if requires == "sponsor":
        return (1 if sponsor in approver_keys else 0, 1)
    # admin-ack, and any TOKEN-bound claim (bearer safety): threshold
    # distinct approvers from {root, sponsor, role:grant holders}.
    admitting = admitting_approvers(
        approver_keys, root=root, sponsor=sponsor, role=role, holds=holds
    )
    return (len(admitting), threshold)


def scope_invite(role: str) -> str:
    return f"invite:{role}"


# -- internal records ----------------------------------------------------------


@dataclass(frozen=True)
class _Grant:
    id: str
    author: str
    child: str
    scopes: FrozenSet[str]
    can_redelegate: bool
    expiry: Optional[int]
    by_root: bool
    hlc_ts: int


@dataclass(frozen=True)
class _Revoke:
    id: str
    author: str
    target_event: Optional[str]
    target_key: Optional[str]


@dataclass(frozen=True)
class _RoleDef:
    id: str
    name: str
    version: int
    scope_set: FrozenSet[str]
    claim_requires: str
    #: Resolved static approver count (D16 tagged value; v1 = static only).
    approver_threshold: int = 1


@dataclass(frozen=True)
class _RoleGrant:
    id: str
    author: str
    persona: str
    role: str
    by_root: bool


@dataclass(frozen=True)
class _RoleRevoke:
    id: str
    persona: str
    role: str


@dataclass(frozen=True)
class _Invite:
    id: str
    author: str
    role: str
    invite_pub: Optional[str]
    token_hash: Optional[str]
    expiry: int
    by_root: bool


@dataclass(frozen=True)
class _Claim:
    id: str
    invite_ref: str
    persona_pub: str
    hlc_ts: int
    kem_credential: Optional[dict] = None


@dataclass(frozen=True)
class _Rekey:
    id: str
    persona: str
    old_pub: str
    new_pub: str
    self_authorized: bool  # signed by the old key itself (not root)


@dataclass(frozen=True)
class _Rotation:
    id: str
    old_pub: str
    new_pub: str


# -- public views ---------------------------------------------------------------


@dataclass(frozen=True)
class RoleDefView:
    name: str
    version: int
    scope_set: tuple
    claim_requires: str
    event_id: str
    approver_threshold: int = 1


@dataclass(frozen=True)
class MemberView:
    persona: str  # persona id == the persona_pub bound at claim time
    current_key: str
    roles: tuple
    sponsor: str
    claim_id: str
    invite_id: str
    #: The claim-carried KEM credential; None when absent or implicitly
    #: retired by a rekey (validity follows the current signing key, §5).
    kem_credential: Optional[dict] = None


class FoldState:
    """The folded authority state at a set of heads. Read-only."""

    def __init__(self, folder: "_Folder"):
        ctx = folder.ctx
        self.heads: tuple = folder.heads
        self.org: str = folder.org
        self.genesis_id: str = folder.genesis_id
        self.root: str = folder.root_at(ctx)
        self.lineage: tuple = tuple(folder.lineage_at(ctx))
        self.valid: Dict[str, bool] = dict(folder.valid)
        self.reasons: Dict[str, str] = dict(folder.reasons)
        held, deleg = folder.authority(ctx, ref_ts=folder.now)
        self._held = held
        self._deleg = deleg
        self.role_defs: Dict[str, RoleDefView] = {
            name: RoleDefView(
                name=d.name,
                version=d.version,
                scope_set=tuple(sorted(d.scope_set)),
                claim_requires=d.claim_requires,
                event_id=d.id,
                approver_threshold=d.approver_threshold,
            )
            for name, d in folder.role_defs_at(ctx).items()
        }
        members, persona_roles = folder.members_at(ctx)
        self.members: Dict[str, MemberView] = {}
        for pid, rec in members.items():
            claim, invite, current_key = rec
            self.members[pid] = MemberView(
                persona=pid,
                current_key=current_key,
                roles=tuple(sorted(persona_roles.get(pid, frozenset()))),
                sponsor=invite.author,
                claim_id=claim.id,
                invite_id=invite.id,
                kem_credential=claim.kem_credential if current_key == pid else None,
            )
        self._bare_roles = {
            pid: tuple(sorted(roles))
            for pid, roles in persona_roles.items()
            if pid not in members
        }
        self.invites: Dict[str, str] = folder.invite_statuses(ctx)
        self.checkpoints: tuple = folder.checkpoints_at(ctx)
        self.loss_heads: tuple = folder.loss_heads_at(ctx)
        self.delegation_parents: Dict[str, tuple] = folder.delegation_parents_at(ctx)

    # -- queries ---------------------------------------------------------------

    def authority(self, key: str) -> frozenset:
        """Scope patterns *key* currently holds (root holds ``{'*'}``)."""
        return self._held.get(key, frozenset())

    def authority_map(self) -> Dict[str, frozenset]:
        """Every key with any held authority → its scope patterns (the
        live-key set; projection layers read this)."""
        return {k: frozenset(v) for k, v in self._held.items() if v}

    def delegable_map(self) -> Dict[str, frozenset]:
        return {k: frozenset(v) for k, v in self._deleg.items() if v}

    @property
    def bare_roles(self) -> Dict[str, tuple]:
        """Role grants to keys that are not claimed member personas."""
        return dict(self._bare_roles)

    def delegable(self, key: str) -> frozenset:
        """Scope patterns *key* may re-delegate."""
        return self._deleg.get(key, frozenset())

    def holds(self, key: str, scope: str) -> bool:
        return set_covers(self._held.get(key, frozenset()), scope)

    def roles(self, persona: str) -> tuple:
        member = self.members.get(persona)
        if member is not None:
            return member.roles
        return self._bare_roles.get(persona, ())

    def fingerprint(self) -> str:
        """SHA-256 over a canonical rendering of the whole state (L1)."""
        state = {
            "org": self.org,
            "root": self.root,
            "lineage": list(self.lineage),
            "heads": list(self.heads),
            "authority": {k: sorted(v) for k, v in sorted(self._held.items()) if v},
            "delegable": {k: sorted(v) for k, v in sorted(self._deleg.items()) if v},
            "role_defs": {
                name: {
                    "version": d.version,
                    "scope_set": list(d.scope_set),
                    "claim_requires": d.claim_requires,
                    "event_id": d.event_id,
                    # Committed in the full TAGGED form so a future kind
                    # changes the state hash by construction (L1).
                    "approver_threshold": {
                        "kind": "static",
                        "count": d.approver_threshold,
                    },
                }
                for name, d in sorted(self.role_defs.items())
            },
            "members": {
                pid: {
                    "current_key": m.current_key,
                    "roles": list(m.roles),
                    "sponsor": m.sponsor,
                    "claim_id": m.claim_id,
                    "invite_id": m.invite_id,
                }
                for pid, m in sorted(self.members.items())
            },
            "bare_roles": {k: list(v) for k, v in sorted(self._bare_roles.items())},
            "invites": dict(sorted(self.invites.items())),
            "checkpoints": list(self.checkpoints),
            "invalid": sorted(i for i, ok in self.valid.items() if not ok),
        }
        return hashlib.sha256(canonical_json(state)).hexdigest()


# -- the folder -----------------------------------------------------------------


class _Folder:
    def __init__(self, ledger: Ledger, heads: Optional[Tuple[str, ...]], now: Optional[int]):
        self.ledger = ledger
        self.now = now
        genesis = ledger.genesis  # raises GenesisError if absent
        self.org = genesis.payload["org"]
        self.genesis_root = genesis.payload["root_pub"]
        self.genesis_id = genesis.event_id

        self.heads = tuple(sorted(set(heads))) if heads is not None else ledger.heads()
        self.ctx = ledger.ancestry(self.heads)
        if self.genesis_id not in self.ctx:
            # Disconnected heads cannot happen structurally, but be explicit.
            self.ctx = self.ctx | frozenset({self.genesis_id})

        self.anc: Dict[str, FrozenSet[str]] = {}
        self.valid: Dict[str, bool] = {}
        self.reasons: Dict[str, str] = {}

        self.grants: Dict[str, _Grant] = {}
        self.revokes: Dict[str, _Revoke] = {}
        self.role_defs: List[_RoleDef] = []
        self.role_grants: Dict[str, _RoleGrant] = {}
        self.role_revokes: Dict[str, _RoleRevoke] = {}
        self.invites: Dict[str, _Invite] = {}
        self.claims: Dict[str, _Claim] = {}
        self.rekeys: Dict[str, _Rekey] = {}
        self.rotations: Dict[str, _Rotation] = {}
        self.checkpoint_ids: List[str] = []

        self._ttl_present = False
        self._auth_cache: Dict[tuple, tuple] = {}
        self._members_cache: Dict[tuple, tuple] = {}
        self._lineage_cache: Dict[frozenset, list] = {}
        self._race_cache: Dict[tuple, bool] = {}

        self._run()

    # -- main pass ---------------------------------------------------------------

    def _topo_order(self) -> List[str]:
        """Parents-first order over ctx, ties broken by event id."""
        pending = {eid: [p for p in self.ledger.get(eid).parents if p in self.ctx] for eid in self.ctx}
        children: Dict[str, List[str]] = {eid: [] for eid in self.ctx}
        indegree: Dict[str, int] = {}
        for eid, parents in pending.items():
            indegree[eid] = len(parents)
            for p in parents:
                children[p].append(eid)
        ready = [eid for eid, d in indegree.items() if d == 0]
        heapq.heapify(ready)
        order = []
        while ready:
            eid = heapq.heappop(ready)
            order.append(eid)
            for child in children[eid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, child)
        return order

    def _run(self) -> None:
        # Nested ancestry evaluations recurse one level per DAG generation.
        limit = 1000 + 20 * len(self.ctx)
        if sys.getrecursionlimit() < limit:
            sys.setrecursionlimit(limit)
        for eid in self._topo_order():
            event = self.ledger.get(eid)
            parent_anc = frozenset()
            for p in event.parents:
                parent_anc = parent_anc | self.anc[p] | {p}
            self.anc[eid] = parent_anc
            handler = getattr(self, "_h_" + event.type.replace(".", "_"))
            reason = handler(event, parent_anc)
            if reason is None:
                self.valid[eid] = True
            else:
                self.valid[eid] = False
                self.reasons[eid] = reason

    def _mark_grant(self, event, by_root: bool) -> None:
        p = event.payload
        expiry = None
        if "ttl" in p:
            expiry = event.hlc.ts + p["ttl"]
            self._ttl_present = True
        self.grants[event.event_id] = _Grant(
            id=event.event_id,
            author=event.author_key,
            child=p["child_pub"],
            scopes=frozenset(p["scope"]),
            can_redelegate=p["can_redelegate"],
            expiry=expiry,
            by_root=by_root,
            hlc_ts=event.hlc.ts,
        )

    # -- per-type handlers (return None when valid, else a reason) ---------------

    def _h_genesis(self, event, ctx) -> Optional[str]:
        return None  # structural rules enforced at Ledger.add

    def _h_delegate(self, event, ctx) -> Optional[str]:
        p = event.payload
        by_root = event.author_key == self.root_at(ctx)
        if not by_root:
            held, deleg = self.authority(ctx, ref_ts=event.hlc.ts)
            scopes = frozenset(p["scope"])
            if not attenuates(scopes, deleg.get(event.author_key, frozenset())):
                if attenuates(scopes, held.get(event.author_key, frozenset())):
                    return R_NOT_REDELEGABLE
                return R_SCOPE_ESCALATION
        self._mark_grant(event, by_root)
        return None

    def _h_revoke(self, event, ctx) -> Optional[str]:
        p = event.payload
        author = event.author_key
        is_root = author == self.root_at(ctx)

        if "target_key" in p:
            target_key = p["target_key"]
            if not (is_root or author == target_key or self._upstream(author, target_key, ctx)):
                return R_REVOKE_UNAUTHORIZED
            self.revokes[event.event_id] = _Revoke(
                id=event.event_id, author=author, target_event=None, target_key=target_key
            )
            return None

        target_id = p["target_event"]
        if target_id not in ctx:
            return R_REVOKE_NOT_IN_ANCESTRY
        if not self.valid.get(target_id, False):
            return R_REVOKE_BAD_TARGET
        target = self.ledger.get(target_id)

        if target.type == "delegate":
            grant = self.grants[target_id]
            allowed = (
                is_root
                or author == grant.author
                or author == grant.child
                or self._upstream(author, grant.author, ctx)
            )
        elif target.type == "invite":
            invite = self.invites[target_id]
            allowed = is_root or author == invite.author or self._upstream(author, invite.author, ctx)
        elif target.type == "role.grant":
            rg = self.role_grants[target_id]
            allowed = (
                is_root
                or author == rg.author
                or author == rg.persona
                or self._upstream(author, rg.author, ctx)
            )
        elif target.type == "member.claim":
            claim = self.claims[target_id]
            sponsor = self.invites[claim.invite_ref].author
            allowed = (
                is_root
                or author == claim.persona_pub
                or author == sponsor
                or self._upstream(author, sponsor, ctx)
            )
        else:
            return R_REVOKE_BAD_TARGET

        if not allowed:
            return R_REVOKE_UNAUTHORIZED
        self.revokes[event.event_id] = _Revoke(
            id=event.event_id, author=author, target_event=target_id, target_key=None
        )
        return None

    def _h_role_define(self, event, ctx) -> Optional[str]:
        p = event.payload
        by_root = event.author_key == self.root_at(ctx)
        if not by_root:
            held, deleg = self.authority(ctx, ref_ts=event.hlc.ts)
            if not set_covers(held.get(event.author_key, frozenset()), SCOPE_ROLE_DEFINE):
                return R_ROLE_DEFINE_UNAUTHORIZED
            # Role scopes flow to holders, so defining a role is a delegation
            # hop: the definer's delegable authority must cover the set (L2).
            if not attenuates(frozenset(p["scope_set"]), deleg.get(event.author_key, frozenset())):
                return R_ROLE_DEFINE_OVERREACH
        self.role_defs.append(
            _RoleDef(
                id=event.event_id,
                name=p["name"],
                version=p["version"],
                scope_set=frozenset(p["scope_set"]),
                claim_requires=p["claim_requires"],
                approver_threshold=p.get(
                    "approver_threshold", {"kind": "static", "count": 1}
                )["count"],
            )
        )
        return None

    def _h_role_grant(self, event, ctx) -> Optional[str]:
        p = event.payload
        if p["role"] not in self.role_defs_at(ctx):
            return R_ROLE_UNDEFINED
        by_root = event.author_key == self.root_at(ctx)
        if not by_root:
            held, _ = self.authority(ctx, ref_ts=event.hlc.ts)
            if not set_covers(held.get(event.author_key, frozenset()), scope_role_grant(p["role"])):
                return R_ROLE_GRANT_UNAUTHORIZED
        self.role_grants[event.event_id] = _RoleGrant(
            id=event.event_id,
            author=event.author_key,
            persona=p["persona"],
            role=p["role"],
            by_root=by_root,
        )
        return None

    def _h_role_revoke(self, event, ctx) -> Optional[str]:
        p = event.payload
        author = event.author_key
        if (
            author != self.root_at(ctx)
            and author != p["persona"]  # self-renounce
            and author != self._current_key_at(p["persona"], ctx)
        ):
            held, _ = self.authority(ctx, ref_ts=event.hlc.ts)
            if not set_covers(held.get(author, frozenset()), scope_role_grant(p["role"])):
                return R_ROLE_REVOKE_UNAUTHORIZED
        self.role_revokes[event.event_id] = _RoleRevoke(
            id=event.event_id, persona=p["persona"], role=p["role"]
        )
        return None

    def _h_invite(self, event, ctx) -> Optional[str]:
        p = event.payload
        if p["sponsor"] != event.author_key:
            return R_INVITE_SPONSOR_MISMATCH
        if p["granted_role"] not in self.role_defs_at(ctx):
            return R_ROLE_UNDEFINED
        by_root = event.author_key == self.root_at(ctx)
        if not by_root:
            held, _ = self.authority(ctx, ref_ts=event.hlc.ts)
            if not set_covers(
                held.get(event.author_key, frozenset()), scope_invite(p["granted_role"])
            ):
                return R_INVITE_OVERREACH
        self.invites[event.event_id] = _Invite(
            id=event.event_id,
            author=event.author_key,
            role=p["granted_role"],
            invite_pub=p.get("invite_pub"),
            token_hash=p.get("token_hash"),
            expiry=p["expiry"],
            by_root=by_root,
        )
        return None

    def _h_member_claim(self, event, ctx) -> Optional[str]:
        p = event.payload
        invite_id = p["invite_ref"]
        if invite_id not in ctx or invite_id not in self.invites:
            return R_INVITE_NOT_IN_ANCESTRY
        invite = self.invites[invite_id]

        if event.hlc.ts > invite.expiry:
            return R_INVITE_EXPIRED
        if not self._invite_usable(invite, ctx, frozenset(), event.hlc.ts):
            return R_INVITE_DEAD

        if invite.invite_pub is not None:
            if event.author_key != invite.invite_pub:
                return R_CLAIM_WRONG_KEY
        else:
            token = p.get("token")
            if (
                token is None
                or hashlib.sha256(token.encode("utf-8")).hexdigest() != invite.token_hash
                or event.author_key != p["persona_pub"]
            ):
                return R_CLAIM_BAD_TOKEN

        # Single use: any surviving claim of this invite in the ancestry wins.
        for claim in self.claims.values():
            if claim.invite_ref == invite_id and claim.id in ctx and self._claim_alive(
                claim, ctx, frozenset()
            ):
                return R_INVITE_ALREADY_CLAIMED

        members, _ = self.members_at(ctx)
        taken = set(members)
        taken.update(rec[2] for rec in members.values())
        if p["persona_pub"] in taken:
            return R_PERSONA_EXISTS

        # Embedded KEM credential (contract §5): no admitted claim carries
        # a credential the fold could not verify with idkit alone.
        credential = p.get("kem_credential")
        if credential is not None:
            binding = {
                k: v for k, v in credential.items() if k not in ("kem_key_id", "signature")
            }
            if (
                hashlib.sha256(canonical_json(binding)).hexdigest()
                != credential["kem_key_id"]
            ):
                return R_CLAIM_BAD_CREDENTIAL
            signed = {**binding, "kem_key_id": credential["kem_key_id"]}
            try:
                verify_signature(
                    p["persona_pub"],
                    credential["signature"],
                    KEM_CREDENTIAL_DOMAIN + canonical_json(signed),
                )
            except IdkitError:
                return R_CLAIM_BAD_CREDENTIAL
            if credential["genesis_id"] != self.genesis_id:
                return R_CLAIM_BAD_CREDENTIAL

        reason = self._check_approvals(event, ctx, invite)
        if reason is not None:
            return reason

        self.claims[event.event_id] = _Claim(
            id=event.event_id,
            invite_ref=invite_id,
            persona_pub=p["persona_pub"],
            hlc_ts=event.hlc.ts,
            kem_credential=credential,
        )
        return None

    def _check_approvals(self, event, ctx, invite: _Invite) -> Optional[str]:
        p = event.payload
        for entry in p["approvals"]:
            try:
                verify_signature(
                    entry["key"], entry["sig"], approval_signing_input("member.claim", p)
                )
            except IdkitError:
                return R_APPROVAL_BAD
        # The acceptance core is claim_requirement_status — shared with
        # the pending-claim readiness seam so the two can never diverge.
        # Bearer safety (register B6) is enforced HERE because the fold is
        # the one layer a headless client cannot bypass: the fold cannot
        # distinguish an email-delivered token from a raw bearer token, so
        # a token-bound claim never self-completes regardless of the
        # role's claim_requires. approver_keys is duplicate-free at
        # validation; authority is evaluated lazily at the claim's ts.
        role_def = self.role_defs_at(ctx)[invite.role]
        held_cache: list = []

        def holds(key: str, scope: str) -> bool:
            if not held_cache:
                held_cache.append(self.authority(ctx, ref_ts=event.hlc.ts)[0])
            return set_covers(held_cache[0].get(key, frozenset()), scope)

        have, need = claim_requirement_status(
            requires=role_def.claim_requires,
            key_bound=invite.invite_pub is not None,
            approver_keys=[entry["key"] for entry in p["approvals"]],
            threshold=role_def.approver_threshold,
            root=self.root_at(ctx),
            sponsor=invite.author,
            role=invite.role,
            holds=holds,
        )
        if have >= need:
            return None
        return R_APPROVAL_MISSING

    def _h_member_rekey(self, event, ctx) -> Optional[str]:
        p = event.payload
        for entry in p["approvals"]:
            try:
                verify_signature(
                    entry["key"], entry["sig"], approval_signing_input("member.rekey", p)
                )
            except IdkitError:
                return R_APPROVAL_BAD
        members, _ = self.members_at(ctx)
        if p["persona"] not in members:
            return R_UNKNOWN_PERSONA
        current = members[p["persona"]][2]
        if p["old_pub"] != current:
            return R_REKEY_WRONG_KEY
        self_authorized = event.author_key == current
        if not self_authorized and event.author_key != self.root_at(ctx):
            return R_REKEY_UNAUTHORIZED
        # Fail closed: a revoked key cannot authorize its own rekey — that
        # would let it rotate to a fresh key and carry its authority out of
        # the revocation. Root-authorized rekey of a compromised member
        # remains valid (that IS the recovery path).
        if self_authorized and self._key_revoked(current, ctx):
            return R_REKEY_REVOKED_KEY
        taken = set(members)
        taken.update(rec[2] for rec in members.values())
        if p["new_pub"] in taken:
            return R_PERSONA_EXISTS
        self.rekeys[event.event_id] = _Rekey(
            id=event.event_id,
            persona=p["persona"],
            old_pub=p["old_pub"],
            new_pub=p["new_pub"],
            self_authorized=self_authorized,
        )
        return None

    def _h_key_rotate(self, event, ctx) -> Optional[str]:
        p = event.payload
        if event.author_key != self.root_at(ctx):
            return R_NOT_ROOT
        if p["old_pub"] != event.author_key:
            return R_ROTATE_WRONG_OLD
        try:
            verify_signature(
                p["new_pub"],
                p["continuity"],
                rotate_continuity_input(p["old_pub"], p["new_pub"]),
            )
        except IdkitError:
            return R_BAD_CONTINUITY
        self.rotations[event.event_id] = _Rotation(
            id=event.event_id, old_pub=p["old_pub"], new_pub=p["new_pub"]
        )
        return None

    def _h_checkpoint(self, event, ctx) -> Optional[str]:
        if event.author_key != self.root_at(ctx):
            held, _ = self.authority(ctx, ref_ts=event.hlc.ts)
            if not set_covers(held.get(event.author_key, frozenset()), SCOPE_CHECKPOINT):
                return R_CHECKPOINT_UNAUTHORIZED
        self.checkpoint_ids.append(event.event_id)
        return None

    # -- root lineage --------------------------------------------------------------

    def lineage_at(self, ctx: frozenset) -> list:
        cached = self._lineage_cache.get(ctx)
        if cached is not None:
            return cached
        lineage = [self.genesis_root]
        used = set()
        while True:
            candidates = [
                rot
                for rid, rot in self.rotations.items()
                if rid in ctx and rid not in used and self.valid[rid] and rot.old_pub == lineage[-1]
            ]
            if not candidates:
                break
            # Concurrent rotations from one root: lowest event hash wins.
            winner = min(candidates, key=lambda r: r.id)
            used.add(winner.id)
            lineage.append(winner.new_pub)
        self._lineage_cache[ctx] = lineage
        return lineage

    def root_at(self, ctx: frozenset) -> str:
        return self.lineage_at(ctx)[-1]

    # -- revocation predicates -------------------------------------------------------

    def _kills(self, view: frozenset) -> List[_Revoke]:
        return [r for rid, r in self.revokes.items() if rid in view and self.valid[rid]]

    def _key_revoked(self, key: str, view: frozenset) -> bool:
        return any(r.target_key == key for r in self._kills(view))

    def _rotation_race(self, eid: str, author: str, view: frozenset) -> bool:
        """A valid rotation moving the root lineage off *author*, concurrent
        with event *eid*. Root-anchored effects lose that race permanently:
        a stolen root key cannot outrun its own rotation (safety bias)."""
        for rid, rot in self.rotations.items():
            if (
                rid in view
                and self.valid[rid]
                and rot.old_pub == author
                and rid not in self.anc[eid]
                and eid not in self.anc[rid]
            ):
                return True
        return False

    def _grant_usable(
        self, g: _Grant, ctx: frozenset, extra: frozenset, ref_ts: Optional[int]
    ) -> bool:
        if g.expiry is not None and ref_ts is not None and g.expiry < ref_ts:
            return False
        view = ctx | extra
        for r in self._kills(view):
            if r.target_event == g.id:
                return False
            # A key-revoke kills every grant to the key except grants issued
            # causally after it (an explicit re-grant survives — L4).
            if r.target_key == g.child and r.id not in self.anc[g.id]:
                return False
        if g.by_root and self._rotation_race(g.id, g.author, view):
            return False
        return not self._race_killed(g, view)

    def _race_killed(self, g: _Grant, view: frozenset) -> bool:
        """Permanently void: a revocation *concurrent* with this grant broke
        its author's supporting authority (revoke beats grant — L3)."""
        if g.by_root:
            return False
        concurrent = frozenset(
            rid
            for rid in view
            if (rid in self.revokes or rid in self.role_revokes)
            and self.valid[rid]
            and rid not in self.anc[g.id]
            and g.id not in self.anc[rid]
        )
        if not concurrent:
            return False
        cache_key = (g.id, concurrent)
        cached = self._race_cache.get(cache_key)
        if cached is not None:
            return cached
        _, deleg = self.authority(self.anc[g.id], extra=concurrent, ref_ts=g.hlc_ts)
        result = not attenuates(g.scopes, deleg.get(g.author, frozenset()))
        self._race_cache[cache_key] = result
        return result

    def _fact_race_killed(self, fact_id: str, author: str, required_scope: str, view: frozenset) -> bool:
        concurrent = frozenset(
            rid
            for rid in view
            if (rid in self.revokes or rid in self.role_revokes)
            and self.valid[rid]
            and rid not in self.anc[fact_id]
            and fact_id not in self.anc[rid]
        )
        if not concurrent:
            return False
        held, _ = self.authority(self.anc[fact_id], extra=concurrent)
        return not set_covers(held.get(author, frozenset()), required_scope)

    def _invite_usable(
        self, invite: _Invite, ctx: frozenset, extra: frozenset, ref_ts: Optional[int]
    ) -> bool:
        view = ctx | extra
        for r in self._kills(view):
            if r.target_event == invite.id:
                return False
            if invite.invite_pub is not None and r.target_key == invite.invite_pub:
                return False
        if ref_ts is not None and invite.expiry < ref_ts:
            return False
        if invite.by_root:
            return not self._rotation_race(invite.id, invite.author, view)
        held, _ = self.authority(ctx, extra=extra, ref_ts=ref_ts)
        return set_covers(held.get(invite.author, frozenset()), scope_invite(invite.role))

    def _concurrent_revocations(self, eid: str, view: frozenset) -> frozenset:
        return frozenset(
            rid
            for rid in view
            if (rid in self.revokes or rid in self.role_revokes)
            and self.valid[rid]
            and rid not in self.anc[eid]
            and eid not in self.anc[rid]
        )

    def _claim_alive(self, claim: _Claim, ctx: frozenset, extra: frozenset) -> bool:
        view = ctx | extra
        for r in self._kills(view):
            if r.target_event == claim.id:
                return False  # membership explicitly revoked
            if r.target_event == claim.invite_ref and claim.id not in self.anc[r.id]:
                return False  # claim-vs-revoke race: revoke wins
        invite = self.invites[claim.invite_ref]
        concurrent = self._concurrent_revocations(claim.id, view)
        # Inviter demotion racing the claim also kills it (safety bias);
        # demotion causally after the claim does not unmake the member.
        return self._invite_usable(invite, self.anc[claim.id], concurrent, claim.hlc_ts)

    def _role_grant_alive(self, rg: _RoleGrant, ctx: frozenset, extra: frozenset) -> bool:
        view = ctx | extra
        for r in self._kills(view):
            if r.target_event == rg.id:
                return False
        for rid, rr in self.role_revokes.items():
            if (
                rid in view
                and self.valid[rid]
                and rr.persona == rg.persona
                and rr.role == rg.role
                and rid not in self.anc[rg.id]  # re-grant after revoke survives
            ):
                return False
        if rg.by_root:
            return not self._rotation_race(rg.id, rg.author, view)
        return not self._fact_race_killed(rg.id, rg.author, scope_role_grant(rg.role), view)

    # -- members & roles ---------------------------------------------------------------

    def members_at(self, ctx: frozenset, extra: frozenset = frozenset()) -> tuple:
        """(members, persona_roles) alive in *ctx*.

        members: persona_id -> (claim, invite, current_key)
        persona_roles: persona-or-bare-key -> frozenset of role names
        """
        cache_key = (ctx, extra)
        cached = self._members_cache.get(cache_key)
        if cached is not None:
            return cached

        alive_by_invite: Dict[str, List[_Claim]] = {}
        for claim in self.claims.values():
            if claim.id in ctx and self._claim_alive(claim, ctx, extra):
                alive_by_invite.setdefault(claim.invite_ref, []).append(claim)

        # Surviving concurrent claims of one invite: lowest event hash wins.
        winners: List[_Claim] = []
        for invite_id in sorted(alive_by_invite):
            winners.append(min(alive_by_invite[invite_id], key=lambda c: c.id))

        # Two concurrent winners binding the same persona key: lowest id wins.
        members: Dict[str, tuple] = {}
        for claim in sorted(winners, key=lambda c: c.id):
            if claim.persona_pub in members:
                continue
            members[claim.persona_pub] = (claim, self.invites[claim.invite_ref])

        # Apply rekey chains (concurrent rekeys: lowest event hash wins).
        result: Dict[str, tuple] = {}
        view = ctx | extra
        for pid, (claim, invite) in members.items():
            current = pid
            used = set()
            while True:
                candidates = [
                    rk
                    for rid, rk in self.rekeys.items()
                    if rid in ctx
                    and rid not in used
                    and self.valid[rid]
                    and rk.persona == pid
                    and rk.old_pub == current
                    and self._rekey_alive(rk, view)
                ]
                if not candidates:
                    break
                winner = min(candidates, key=lambda r: r.id)
                used.add(winner.id)
                current = winner.new_pub
            result[pid] = (claim, invite, current)

        persona_roles: Dict[str, set] = {}
        for pid, (claim, invite, _current) in result.items():
            if self._claim_role_alive(claim, invite, ctx, extra):
                persona_roles.setdefault(pid, set()).add(invite.role)
        for rg in self.role_grants.values():
            if rg.id in ctx and self.valid[rg.id] and self._role_grant_alive(rg, ctx, extra):
                persona_roles.setdefault(rg.persona, set()).add(rg.role)

        out = (result, {k: frozenset(v) for k, v in persona_roles.items()})
        self._members_cache[cache_key] = out
        return out

    def _rekey_alive(self, rk: _Rekey, view: frozenset) -> bool:
        """A self-authorized rekey loses a race with the revocation of its
        authorizing key (a revoked key cannot outrun revocation by rekeying);
        an ancestral revoke already made it issuance-invalid, and a revoke
        causally *after* the rekey targets an abandoned key — a no-op.
        Root-authorized rekeys are the recovery path and never race-lose."""
        if not rk.self_authorized:
            return True
        for r in self._kills(view):
            if (
                r.target_key == rk.old_pub
                and r.id not in self.anc[rk.id]
                and rk.id not in self.anc[r.id]
            ):
                return False
        return True

    def _claim_role_alive(self, claim: _Claim, invite: _Invite, ctx, extra) -> bool:
        """The invite-granted role can be stripped by role.revoke without
        unmaking the membership."""
        view = ctx | extra
        for rid, rr in self.role_revokes.items():
            if (
                rid in view
                and self.valid[rid]
                and rr.persona == claim.persona_pub
                and rr.role == invite.role
                and rid not in self.anc[claim.id]
            ):
                return False
        return True

    def role_defs_at(self, ctx: frozenset) -> Dict[str, _RoleDef]:
        """LWW per name: highest version wins; equal versions break by hash."""
        best: Dict[str, _RoleDef] = {}
        for d in self.role_defs:
            if d.id not in ctx or not self.valid[d.id]:
                continue
            cur = best.get(d.name)
            if cur is None or (d.version, _neg_id(d.id)) > (cur.version, _neg_id(cur.id)):
                best[d.name] = d
        return best

    # -- the authority fixpoint ---------------------------------------------------------

    def authority(
        self, ctx: frozenset, extra: frozenset = frozenset(), ref_ts: Optional[int] = None
    ) -> tuple:
        """(held, deleg) maps key -> frozenset of scope patterns, at *ctx*.

        Standing delegations contribute only while a live chain connects
        them to the root lineage (the cascade, L4). *extra* injects
        revocation events from outside *ctx* for race evaluation.
        """
        if not self._ttl_present:
            ref_ts = None
        cache_key = (ctx, extra, ref_ts)
        cached = self._auth_cache.get(cache_key)
        if cached is not None:
            return cached

        held: Dict[str, frozenset] = {}
        deleg: Dict[str, frozenset] = {}
        root = self.root_at(ctx)
        held[root] = held.get(root, frozenset()) | UNIVERSE
        deleg[root] = deleg.get(root, frozenset()) | UNIVERSE

        # Role-held scopes (non-delegable) attach to current persona keys.
        members, persona_roles = self.members_at(ctx, extra)
        defs = self.role_defs_at(ctx)
        view = ctx | extra
        for pid, roles in persona_roles.items():
            key = members[pid][2] if pid in members else pid
            if self._key_revoked(key, view):
                continue
            scopes = frozenset()
            for role in roles:
                d = defs.get(role)
                if d is not None:
                    scopes |= d.scope_set
            if scopes:
                held[key] = held.get(key, frozenset()) | scopes

        usable = [
            g
            for gid, g in self.grants.items()
            if gid in ctx and self.valid[gid] and self._grant_usable(g, ctx, extra, ref_ts)
        ]
        changed = True
        while changed:
            changed = False
            for g in usable:
                if g.by_root:
                    eff = g.scopes
                else:
                    d = deleg.get(g.author)
                    if not d:
                        continue
                    eff = covered_subset(g.scopes, d)
                if not eff:
                    continue
                if not eff <= held.get(g.child, frozenset()):
                    held[g.child] = held.get(g.child, frozenset()) | eff
                    changed = True
                if g.can_redelegate and not eff <= deleg.get(g.child, frozenset()):
                    deleg[g.child] = deleg.get(g.child, frozenset()) | eff
                    changed = True

        result = (held, deleg)
        self._auth_cache[cache_key] = result
        return result

    def _upstream(self, author: str, key: str, ctx: frozenset) -> bool:
        """Provenance: is *key* downstream of *author* — via delegations,
        invites, or sponsored claims?

        Follows issuance-valid edges regardless of later revocation —
        revocation authority tracks who granted what, and stays with the
        granter even after intermediate links were themselves revoked
        (attenuation-safe: revoking can only remove authority).
        """
        edges: Dict[str, set] = {}
        for gid, g in self.grants.items():
            if gid in ctx and self.valid[gid]:
                edges.setdefault(g.author, set()).add(g.child)
        for iid, invite in self.invites.items():
            if iid in ctx and self.valid[iid] and invite.invite_pub is not None:
                edges.setdefault(invite.author, set()).add(invite.invite_pub)
        for cid, claim in self.claims.items():
            if cid in ctx and self.valid[cid]:
                sponsor = self.invites[claim.invite_ref].author
                edges.setdefault(sponsor, set()).add(claim.persona_pub)
        frontier = {author}
        seen = set()
        while frontier:
            cur = frontier.pop()
            if cur == key:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            frontier.update(edges.get(cur, set()) - seen)
        return False

    def _current_key_at(self, persona: str, ctx: frozenset) -> Optional[str]:
        members, _ = self.members_at(ctx)
        rec = members.get(persona)
        return rec[2] if rec is not None else None

    # -- final-state views -----------------------------------------------------------------

    def invite_statuses(self, ctx: frozenset) -> Dict[str, str]:
        members, _ = self.members_at(ctx)
        claimed = {rec[1].id for rec in members.values()}
        statuses: Dict[str, str] = {}
        for iid, invite in self.invites.items():
            if iid not in ctx or not self.valid[iid]:
                continue
            if iid in claimed:
                statuses[iid] = INVITE_CLAIMED
                continue
            directly_revoked = any(
                r.target_event == iid
                or (invite.invite_pub is not None and r.target_key == invite.invite_pub)
                for r in self._kills(ctx)
            )
            if directly_revoked:
                statuses[iid] = INVITE_REVOKED
            elif self.now is not None and invite.expiry < self.now:
                statuses[iid] = INVITE_EXPIRED
            elif not self._invite_usable(invite, ctx, frozenset(), self.now):
                statuses[iid] = INVITE_DEAD  # unclaimed invites die with their inviter
            else:
                statuses[iid] = INVITE_LIVE
        return statuses

    def checkpoints_at(self, ctx: frozenset) -> tuple:
        return tuple(sorted(cid for cid in self.checkpoint_ids if cid in ctx and self.valid[cid]))

    def delegation_parents_at(self, ctx: frozenset) -> Dict[str, tuple]:
        """Delegation edges the authority fixpoint admits: each grant child
        key -> the sorted authors of its issuance-valid, usable grants at
        *ctx*. Read-only view for chain resolution (storage acceptance
        resolves a delegated actor key upward to a member persona)."""
        parents: Dict[str, set] = {}
        for gid, g in self.grants.items():
            if gid in ctx and self.valid[gid] and self._grant_usable(g, ctx, frozenset(), self.now):
                parents.setdefault(g.child, set()).add(g.author)
        return {child: tuple(sorted(authors)) for child, authors in parents.items()}

    def loss_heads_at(self, ctx: frozenset) -> tuple:
        """The maximal valid access contractions in *ctx* (RequiredLossHeads).

        Candidates: every valid ``revoke`` (event- and key-target),
        ``role.revoke``, ``member.rekey``, and ``key.rotate``, plus each
        valid ``role.define`` that drops at least one scope relative to
        ANY same-name definition in *ctx* it outranks in the LWW order
        ``(version, lower-hash-wins)`` — judged against the frontier, not
        the candidate's own ancestry, so a narrowing that only becomes
        effective at a merge (concurrent branches, equal-version
        tie-breaks) is still a contraction. Scope removal is decided by
        the coverage order (``attenuates``), not raw set difference, so a
        redefinition to a covering pattern is not a contraction.
        Deliberately over-inclusive in the safe direction (§6). Version
        one has a single org-wide domain, so no per-domain filter applies.
        Maximality is over the causal-ancestry map: a candidate ancestral
        to another candidate is dominated. Sorted by event id.
        """
        candidates = set()
        for group in (self.revokes, self.role_revokes, self.rekeys, self.rotations):
            candidates.update(eid for eid in group if eid in ctx and self.valid[eid])
        defs_in_ctx = [d for d in self.role_defs if d.id in ctx and self.valid[d.id]]
        for d in defs_in_ctx:
            d_rank = (d.version, _neg_id(d.id))
            if any(
                p.name == d.name
                and p.id != d.id
                and (p.version, _neg_id(p.id)) < d_rank
                and not attenuates(p.scope_set, d.scope_set)
                for p in defs_in_ctx
            ):
                candidates.add(d.id)
        return tuple(
            sorted(
                c
                for c in candidates
                if not any(c in self.anc[o] for o in candidates if o != c)
            )
        )


def _neg_id(event_id: str) -> tuple:
    """Sort helper: makes 'lower hash wins' usable inside a max()."""
    return tuple(-b for b in bytes.fromhex(event_id))


def fold(ledger: Ledger, heads=None, now: Optional[int] = None) -> FoldState:
    """Fold *ledger* into an authority state.

    - *heads*: fold only the ancestry of these event ids (default: all
      current heads — the full DAG).
    - *now*: optional unix-ms timestamp for expiry evaluation (invite
      expiry, delegation TTLs). Omitted ⇒ time-independent state; the fold
      never reads the wall clock itself (L1 determinism).
    """
    return FoldState(_Folder(ledger, tuple(heads) if heads is not None else None, now))
