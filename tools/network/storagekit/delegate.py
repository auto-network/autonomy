"""The storage agent delegate — an attenuated, TTL-bounded signing key
that lets an unattended process advance generations and issue capability
grants *as* a member persona, with no human in the loop per action.

Why this exists (``1e005d5c-c11`` §9, §11; ``0c206bd8-1c6`` §7.4). The
dashboard's ``audited`` writes and the paired re-key (§1d) must SIGN a
record — a ``StorageStateDescriptor`` (a generation advance) or a
``CapabilityGrant``. The browser session key cannot: it is
``extractable:false`` and browser-bound, so a server-side process can
never hold it. The pattern that solves exactly this ships for the tunnel
in ``network-signon.mjs`` (``_mintServeCredential`` / ``provisionServeCert``,
lines 439-498): mint a fresh child ``extractable:true``, certify it under
a higher authority, attenuate its scope, bound its lifetime, and export
its private half to the unattended connector. This module is the storage
equivalent — but delegated from a MEMBER PERSONA, not the org root, and
enforced by THE FOLD at acceptance rather than an in-process check.

What it is NOT (§22, B7). This is not the org-root-delegated hot key B7
retracted. That carried constitutional, unbounded (``*``) authority and
was enforced by an ``if`` statement — no boundary against an attacker who
controls the code path. This delegate is:

  * **persona-delegated** — its delegation chain terminates at a member
    persona; a chain resolving outside the roster is void (PIN 6b);
  * **scope-attenuated** — it carries EXACTLY ``storage:state:advance:<domain>``
    and ``storage:capability:grant:<domain>``, and nothing else. Never
    ``checkpoint`` (the frontier marker is authored at a root-present
    login), never an intent scope (``role:grant`` / ``invite`` /
    ``link:publish`` / ``link:revoke`` — B7 requires per-action user
    approval), never ``*``;
  * **TTL-bounded** — the ledger ``delegate`` event carries a ``ttl``; the
    fold refuses its records once ``hlc.ts + ttl`` passes;
  * **fold-enforced** — every honest node resolves the record's signer up
    to a member persona (:func:`acceptance.resolve_member_key`) and checks
    THAT persona's membership. The boundary is the fold, on every node,
    never local code.

How the fold enforces "exactly two scopes" (the non-obvious part).
Storage acceptance never reads the delegate's declared scopes — it only
resolves the signer's chain to a member. The scope discipline is enforced
one hop earlier, at delegation-event admission, by the bounded
self-delegation rule (auto-wrkaq): a CURRENT member persona may mint a
strictly weaker, NON-redelegable, EXPIRING instrument of scopes it holds
— through a role, typically — restricted by ``ledger.scopes.self_delegable_exact``
(exactly the two storage scope families, because their acceptance
re-derives authority from current membership at use time). A mint that
reaches beyond the author's held scopes, beyond that set, omits the
``ttl``, or sets ``can_redelegate`` fails admission
(``R_SCOPE_ESCALATION`` / ``R_NOT_REDELEGABLE``), no delegation edge
enters the fold, and the signer no longer resolves to a member — so the
storage records that key signs are refused by every honest node. An
issuer that is not itself a member persona yields the same refusal from
the other direction: the chain terminates outside the roster, and the
persona condition is also what pins the chain at depth one (a delegate
still HOLDS its scopes, but a delegate is not a persona and cannot use
this rule to mint onward).

Pure over the ledger it is handed: it appends authority events and signs
records, and never touches the filesystem. The MEMORY-class home for the
returned private key (ramfs, never disk) is :class:`RamDelegateCache`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from tools.network.idkit import KeyPair
from tools.network.ledger import HLC, make_event, mint_grant_nonce, sign_delegate_proof
from tools.network.ledger.projections import organization_content_domain_id

from . import capability as capability_mod
from . import state as state_mod
from .acceptance import scope_storage_advance, scope_storage_grant
from .errors import StorageError

#: A warm-period-bounded default TTL (ms). A delegate is re-provisioned at
#: the next unlock, so a short life keeps the exposure window tight; the
#: caller may shorten or lengthen it, but the fold — not this default — is
#: the boundary.
# Owned by tools.network.clock (a validity interval, ms); re-exported here.
from tools.network.clock import DEFAULT_DELEGATE_TTL_MS


class DelegateError(StorageError):
    """A storage-delegate provisioning or lifecycle refusal."""


def _key_hex(value) -> str:
    return value.public_hex if isinstance(value, KeyPair) else value


def storage_delegate_scopes(domain_id: str) -> list:
    """The EXACTLY-two execution scopes a storage agent delegate may carry.

    Sorted, so the ledger ``delegate`` event's ``scope`` list is canonical.
    This is the whole of the delegate's reach: never ``checkpoint``, never
    an intent scope, never ``*`` (§8, §9).
    """
    return sorted([scope_storage_advance(domain_id), scope_storage_grant(domain_id)])


@dataclass(frozen=True)
class StorageDelegate:
    """A provisioned agent delegate: its private signing key (MEMORY-class,
    to be held in ramfs) plus the public facts of its delegation.

    ``signing_key`` is the only secret; everything else is public and is
    recomputable from the ledger. ``member_persona`` is the roster key the
    delegation chain terminates at — the persona the unattended process
    acts *as*.
    """

    signing_key: KeyPair
    child_pub: str
    member_persona: str
    genesis_id: str
    domain_id: str
    scopes: tuple
    grant_event_id: str
    issued_ts: int
    not_after: int

    @property
    def public_hex(self) -> str:
        return self.child_pub

    def live_at(self, now_ms: int) -> bool:
        """Advisory: whether the TTL still covers ``now_ms``. The fold makes
        the binding decision — this only mirrors it for callers deciding
        when to renew."""
        return now_ms < self.not_after


def _parents(store, parents):
    return list(store.heads()) if parents is None else list(parents)


def provision(
    store,
    issuer: KeyPair,
    member_persona,
    genesis_id: str,
    *,
    hlc: HLC,
    ttl_ms: int = DEFAULT_DELEGATE_TTL_MS,
    domain_id: Optional[str] = None,
    parents=None,
    signing_key: Optional[KeyPair] = None,
) -> StorageDelegate:
    """Mint a fresh storage agent delegate.

    Generates a fresh signing key (unless ``signing_key`` is supplied — the
    renewal path reuses the child), appends a ``delegate`` event THROUGH
    ``store`` from ``issuer`` granting EXACTLY the two storage scopes to that
    key, bounded by ``ttl_ms``, and returns the :class:`StorageDelegate` holding
    the private half for placement in the MEMORY-class cache.

    ``store`` is the ledger the event is appended to. Pass the durable
    :class:`~tools.network.ledger.store.LedgerStore` — NOT its in-memory
    ``.ledger`` — or the delegate exists only in this process and vanishes the
    moment anything re-opens the ledger from disk, which a per-call key holder
    or sealer does every time. A bare in-memory ledger is for simulation only;
    both answer the same ``append`` / ``genesis_id`` / ``heads`` interface, so
    this function does not care which it is handed, only that a durable caller
    hands it the store.

    ``issuer`` is the key that signs the ``delegate`` event — a CURRENT
    member persona holding the two storage scopes, typically through a
    role: the fold's bounded self-delegation rule (auto-wrkaq) admits a
    persona minting a strictly weaker, non-redelegable, expiring
    instrument of its own held authority, so no root-present enabling act
    exists or is needed. ``member_persona`` is the roster key the chain
    must terminate at, and is carried on the result for the caller's
    records only.

    This never pre-judges usability. Whether the delegate can author
    storage records is decided by the fold at acceptance: an issuer without
    delegable storage authority, an over-reaching scope, or a missing
    ``ttl`` all yield an edge the fold refuses. The mint always carries a
    ``ttl`` (there is no unbounded storage delegate) and never
    ``can_redelegate`` (the agent is a leaf — it may not sub-delegate).
    """
    if ttl_ms <= 0:
        raise DelegateError("a storage delegate must carry a positive TTL")
    if domain_id is None:
        domain_id = organization_content_domain_id(genesis_id)
    key = signing_key if signing_key is not None else KeyPair.generate()
    # A fresh nonce EVERY mint, renewals included: each grant is a new
    # consent, and the child key is in hand here so the fresh signature
    # costs nothing (auto-le0kg) — the renewal path reuses the child key
    # it is extending and still signs anew.
    nonce = mint_grant_nonce()
    payload = {
        "type": "delegate",
        "child_pub": key.public_hex,
        "scope": storage_delegate_scopes(domain_id),
        "can_redelegate": False,
        "ttl": int(ttl_ms),
        "grant_nonce": nonce,
        "proof": sign_delegate_proof(
            key, store.genesis_id, issuer.public_hex,
            storage_delegate_scopes(domain_id),
            can_redelegate=False, ttl=int(ttl_ms), grant_nonce=nonce,
        ),
    }
    grant_id = store.append(make_event(issuer, payload, _parents(store, parents), hlc))
    return StorageDelegate(
        signing_key=key,
        child_pub=key.public_hex,
        member_persona=_key_hex(member_persona),
        genesis_id=genesis_id,
        domain_id=domain_id,
        scopes=tuple(storage_delegate_scopes(domain_id)),
        grant_event_id=grant_id,
        issued_ts=hlc.ts,
        not_after=hlc.ts + int(ttl_ms),
    )


def renew(
    store,
    issuer: KeyPair,
    delegate: StorageDelegate,
    *,
    hlc: HLC,
    ttl_ms: int = DEFAULT_DELEGATE_TTL_MS,
    parents=None,
) -> StorageDelegate:
    """Extend a delegate before it expires: append a fresh ``delegate``
    event for the SAME child key with a later expiry.

    The fold treats any usable grant to the child as a live edge, so a
    renewal issued while the old grant is still usable keeps acceptance
    unbroken across the boundary. The signing key is unchanged; only the
    authority window moves. Returns the updated :class:`StorageDelegate`.
    """
    renewed = provision(
        store,
        issuer,
        delegate.member_persona,
        delegate.genesis_id,
        hlc=hlc,
        ttl_ms=ttl_ms,
        domain_id=delegate.domain_id,
        parents=parents,
        signing_key=delegate.signing_key,
    )
    return renewed


def revoke(
    store,
    issuer: KeyPair,
    delegate: StorageDelegate,
    *,
    hlc: HLC,
    parents=None,
    reason: Optional[str] = None,
) -> str:
    """Retire a delegate immediately: append a key-target ``revoke`` event
    for the delegate's child key.

    A key-revoke kills every grant to the child (except one issued causally
    after it), so the delegation edge vanishes from the fold the moment the
    revoke enters a node's view — the signer stops resolving to a member and
    its storage records are refused everywhere, ahead of the TTL. The issuer
    must be the root, the delegate's own key, or upstream of it (the member
    persona that minted it qualifies). This is the fold-enforced revocation
    the §9 descriptor names; it is not a ceremony. Returns the event id.
    """
    payload = {"type": "revoke", "target_key": delegate.child_pub}
    if reason is not None:
        payload["reason"] = reason
    return store.append(make_event(issuer, payload, _parents(store, parents), hlc))


# -- signing the unattended records ---------------------------------------------------


def sign_generation_advance(
    delegate: StorageDelegate,
    *,
    parent_state_ids=(),
    authority_heads,
    covered_loss_heads,
    loss_projection_digest: str,
) -> tuple:
    """Sign a fresh generation descriptor with the delegate key
    (``storage:state:advance``). Returns ``(descriptor, state_secret)``.

    This is the unattended re-key / advance path. The descriptor's
    ``creator_persona`` is the delegate's public key; the fold resolves it
    up to ``member_persona`` at acceptance.
    """
    return state_mod.generate(
        delegate.signing_key,
        delegate.genesis_id,
        delegate.domain_id,
        parent_state_ids,
        authority_heads,
        covered_loss_heads,
        loss_projection_digest,
    )


def sign_capability_grant(
    delegate: StorageDelegate,
    *,
    storage_state_id: str,
    recipient_credential,
    state_secret: bytes,
    state_secret_commitment: str,
    authority_heads,
) -> capability_mod.CapabilityGrant:
    """Seal a state secret to a recipient and sign the grant with the
    delegate key (``storage:capability:grant``).

    The grant's ``grantor_persona`` is the delegate's public key; the fold
    resolves it up to ``member_persona`` at acceptance.
    """
    return capability_mod.issue(
        delegate.signing_key,
        genesis_id=delegate.genesis_id,
        domain_id=delegate.domain_id,
        storage_state_id=storage_state_id,
        recipient_credential=recipient_credential,
        state_secret=state_secret,
        state_secret_commitment=state_secret_commitment,
        authority_heads=authority_heads,
    )
