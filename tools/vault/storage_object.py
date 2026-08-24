"""A setting revision as a storage-domain object.

An ordinary setting keeps its payload as plain JSON in the organization's
database. That is right for almost everything and wrong for a credential:
whatever can read the file can read the secret, and the file is readable by
design — a host process opens it directly and the repository is mounted into
containers on purpose. Access control cannot close that surface, so the answer
is that the bytes in the row stop being the secret.

A vault setting stores a **locator**. The payload is encrypted once as a
content object under the organization's current key generation
(``0c206bd8-1c6`` §7, §9), and the row keeps only what finds it again. No key
material and no ciphertext: a row carrying either would put the secret back in
the file this exists to keep it out of.

**The mapping.** A setting is already an immutable record — a change appends a
superseding row rather than editing (``settings_ops.override_setting``), and
``read_set`` composes at read time — which is exactly what the contract
requires of an object. So one setting is one object and one row is one
revision, and both identifiers are DERIVED rather than stored: every revision
of one setting must land on the same object, and two nodes must agree on which
without asking each other.

**Two tiers, nested in this order** (§9.2). The storage state governs
membership — holding its secret means "I am an authorized member of this
domain". The policy class governs the human factor — holding its key means a
password (or, when the factor exists, a passkey PRF) was presented.

* ``audited``  — the storage state alone yields the content key. Released to
  any authorized session, so the boundary is ACCOUNTABILITY, not
  confidentiality. This is what the dashboard's cached state secret (§6)
  releases with no human present.
* ``secured``  — the object body is the policy-wrapped content key and the
  ciphertext it opens. A member holding the state opens the object and gets
  the wrapped key and nothing else; opening THAT needs the class, which needs
  the factor. The nesting is what makes a cached state secret insufficient by
  construction rather than by a check code could omit.

Composes built code and adds no cryptographic construction: object creation,
selection and advancement come from ``tools/network/storagekit``, the class
wrap from ``tools.vault.policy_class``, and the inner body seal is the same
``object_header.seal_body`` the outer layer uses, under a different key.
"""

from __future__ import annotations


import base64
import hashlib
import json
import os
from dataclasses import dataclass, field as dataclass_field, replace as dataclass_replace

from tools.network.idkit.canonical import canonical_json
from tools.network.ledger.projections import organization_content_domain_id
from tools.network.storagekit import (
    acceptance,
    bridge as bridge_mod,
    distribution,
    lifecycle,
    object_header,
    objects,
    state as state_mod,
    suites,
)
from tools.network.storagekit.errors import StorageError
from tools.network.storagekit.lifecycle import StateAdvanceRequired

from .errors import VaultError
from .policy_class import open_cek, seal_cek

#: TIER (design of record §4.1) — WHO MUST PARTICIPATE. Distinct from the
#: policy class, which is the human-factor key and a separate axis; conflating
#: them is how a setting ends up labelled password-protected while nothing
#: enforces a password.
AUDITED = "audited"
SECURED = "secured"
TIERS = frozenset({AUDITED, SECURED})

#: Version tag opening every locator. The locator is an OPAQUE SCALAR, so this
#: is a prefix on a string rather than a field in an object — see
#: :func:`build_locator` for why that distinction is load-bearing.
LOCATOR_PREFIX = "autonomy.vault.v1."

#: Domain separation for the two derived identifiers. Distinct strings, so an
#: object id can never collide with a revision id derived from the same row.
_OBJECT_ID_DOMAIN = "autonomy/vault/object-id/v1"
_REVISION_ID_DOMAIN = "autonomy/vault/revision-id/v1"

#: The envelope version for a ``secured`` object body.
_SECURED_ENVELOPE_VERSION = 1

_LOCATOR_FIELDS = frozenset(
    {
        "genesis_id",
        "domain_id",
        "object_id",
        "revision_id",
        "storage_state_id",
        "tier",
        "policy_class_id",
        "required_policy",
    }
)

_ID_FIELDS = (
    "genesis_id",
    "domain_id",
    "object_id",
    "revision_id",
    "storage_state_id",
)


# ── the derived identifiers ───────────────────────────────────────────────


def object_id_for(genesis_id: str, set_id: str, key: str) -> str:
    """The object every revision of one setting belongs to.

    A function of the organization and the setting's identity, so it is stable
    across revisions, distinct across settings, and distinct across
    organizations — and two nodes compute the same value without coordinating.
    """
    return hashlib.sha256(
        canonical_json(
            {
                "domain": _OBJECT_ID_DOMAIN,
                "genesis_id": genesis_id,
                "set_id": set_id,
                "key": key,
            }
        )
    ).hexdigest()


def revision_id_for(genesis_id: str, setting_id: str) -> str:
    """The identity of ONE revision — one settings row.

    Derived from the row's own id, which is unique per row, so writing the
    same setting twice yields two revisions of one object. That is what an
    append-only history should look like, and it is why a vault setting is
    written by appending a row rather than rewriting one: rewriting a row
    would re-derive the same revision, and a committed revision admits only a
    byte-identical replay (``storagekit.store``, contract Invariant 7).
    """
    return hashlib.sha256(
        canonical_json(
            {
                "domain": _REVISION_ID_DOMAIN,
                "genesis_id": genesis_id,
                "setting_id": setting_id,
            }
        )
    ).hexdigest()


# ── the locator ───────────────────────────────────────────────────────────


def build_locator(
    *,
    genesis_id: str,
    domain_id: str,
    object_id: str,
    revision_id: str,
    storage_state_id: str,
    tier: str,
    policy_class_id: str | None = None,
    required_policy: str | None = None,
) -> str:
    """The OPAQUE SCALAR a vault setting stores in place of its payload.

    A scalar, and that is a correctness requirement rather than a preference.
    Settings resolution merges candidate rows with RFC 7386 ``json_merge_patch``
    BEFORE anything is decrypted (crib §17), and merge-patch RECURSES INTO
    OBJECTS. A locator shaped as an object would be merged field by field, so
    a partial override could yield one carrying the object id from one write
    and the revision id from another — addressing an object that was never
    written and deriving a wrap key for it. As a scalar it can only be
    replaced whole, which is the behaviour that step must have.

    The tier and the class reference travel INSIDE for the same reason: a
    sibling field could be merged in from another row, and a tier that can be
    overridden separately from the thing it describes is a downgrade waiting
    to happen.
    """
    if tier not in TIERS:
        raise VaultError(f"unknown tier {tier!r}; this build provides {sorted(TIERS)}")
    for name, value in (
        ("genesis_id", genesis_id),
        ("domain_id", domain_id),
        ("object_id", object_id),
        ("revision_id", revision_id),
        ("storage_state_id", storage_state_id),
    ):
        if not isinstance(value, str) or not value:
            raise VaultError(f"vault locator {name} must be a non-empty string")
    if tier == SECURED:
        if not policy_class_id or not required_policy:
            raise VaultError(
                "a secured locator names its policy class and the policy it "
                "requires; without them nothing states which human factor opens it"
            )
    elif policy_class_id is not None or required_policy is not None:
        raise VaultError(
            "an audited locator carries no policy class — the storage state "
            "alone yields its content key, and naming a class it does not use "
            "would assert a human factor nothing enforces"
        )
    body = canonical_json(
        {
            "genesis_id": genesis_id,
            "domain_id": domain_id,
            "object_id": object_id,
            "revision_id": revision_id,
            "storage_state_id": storage_state_id,
            "tier": tier,
            "policy_class_id": policy_class_id,
            "required_policy": required_policy,
        }
    )
    return LOCATOR_PREFIX + base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def is_vault_locator(payload) -> bool:
    """Whether a settings payload is a locator rather than a value."""
    return isinstance(payload, str) and payload.startswith(LOCATOR_PREFIX)


def parse_locator(payload) -> dict:
    """Strictly read a locator back.

    Closed to an exact field set, like the armor body: a tolerated extra field
    would be somewhere to smuggle something into a row that resolution trusts.
    """
    if not is_vault_locator(payload):
        raise VaultError("this settings payload is not a vault locator")
    encoded = payload[len(LOCATOR_PREFIX) :]
    padding = "=" * (-len(encoded) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except Exception as exc:  # noqa: BLE001 — any decode failure is one refusal
        raise VaultError(f"vault locator does not decode: {exc}") from exc
    if not isinstance(data, dict) or set(data) != _LOCATOR_FIELDS:
        raise VaultError(
            f"a vault locator must carry exactly {sorted(_LOCATOR_FIELDS)} — got "
            f"{sorted(data) if isinstance(data, dict) else type(data).__name__}"
        )
    if data["tier"] not in TIERS:
        raise VaultError(
            f"unknown tier {data['tier']!r}; refusing to resolve a secret whose "
            "release rule this build does not implement"
        )
    for name in _ID_FIELDS:
        if not isinstance(data[name], str) or not data[name]:
            raise VaultError(f"vault locator {name} must be a non-empty string")
    if data["tier"] == SECURED:
        if not data["policy_class_id"] or not data["required_policy"]:
            raise VaultError("a secured locator must name its policy class")
    elif data["policy_class_id"] is not None or data["required_policy"] is not None:
        raise VaultError("an audited locator must not name a policy class")
    return data


# ── what a writer holds ───────────────────────────────────────────────────


@dataclass
class Holdings:
    """Exactly what this writer has of the domain's key control.

    ``secrets`` are the state secrets it holds directly (mint seeds and
    accepted capability grants); ``descriptors`` and ``bridges`` are the
    public records it has seen. Kept as one object because the storage layer
    takes all three at once and separating them at every call site is how they
    drift apart.
    """

    secrets: dict = dataclass_field(default_factory=dict)
    descriptors: dict = dataclass_field(default_factory=dict)
    bridges: list = dataclass_field(default_factory=list)

    def reachable_secrets(self) -> dict:
        """Every state secret this writer can produce — held directly, or
        recovered backward from a held descendant through parent bridges,
        each hop commitment-checked inside the bridge module."""
        reachable = dict(self.secrets)
        for state_id, secret in sorted(self.secrets.items()):
            try:
                reachable.update(
                    bridge_mod.recover_ancestors(
                        state_id, secret, self.bridges, self.descriptors
                    )
                )
            except StorageError:
                # A poisoned or unopenable bridge body costs its edge, not the
                # write: the states still reachable by other paths remain.
                continue
        return reachable

    def usable_states(self) -> dict:
        """The descriptors whose secret this writer can actually produce.

        ``select_safe_state`` picks the smallest identifier among the states
        it is offered, with no view of what the caller holds — so offering it
        a safe state whose secret is unreachable makes the write fail on a
        state that was never a candidate for THIS writer. Selection is a
        property of the writer, so the candidate set is too.
        """
        reachable = self.reachable_secrets()
        return {
            state_id: descriptor
            for state_id, descriptor in self.descriptors.items()
            if state_id in reachable
        }

    def usable_heads(self) -> list:
        """The maximal usable states — the ones to bridge a new state to.

        A state that some other usable state already names as a parent is
        reachable through it, so bridging to it as well adds an edge that
        carries nothing.
        """
        usable = self.usable_states()
        covered = {
            parent
            for descriptor in usable.values()
            for parent in descriptor.parent_state_ids
        }
        return [d for state_id, d in sorted(usable.items()) if state_id not in covered]


@dataclass(frozen=True)
class StateAdvance:
    """A key generation minted in line with a write.

    ``descriptor`` and ``bridges`` are public and go to the broker;
    ``secret`` is the generation key itself and goes only into sealed
    capability grants. ``grants`` are already sealed to their recipients.
    """

    descriptor: object
    secret: bytes
    bridges: tuple
    grants: tuple = ()


@dataclass(frozen=True)
class SealedRevision:
    """One setting revision, encrypted. ``locator`` is what the row keeps."""

    locator: str
    header: object
    body: bytes
    advance: StateAdvance | None = None


# ── the write path (contract §10) ──────────────────────────────────────────


def _advance_in_line(author, domain_id, genesis_id, frontier, holdings, recipient_credentials):
    """Mint the generation this write needs, here, now.

    The gate is precisely that the writer holds the secret of a state whose
    covered-loss set includes every access contraction in its cited authority
    frontier (§7.4). Everything that gate requires is LOCAL: the fold is
    already computed, the key is 256 bits from the OS CSPRNG, and the
    descriptor is signed by the writer. So the writer holds the new secret by
    construction, having just generated it, and the write proceeds in the same
    operation — no round trip, no quorum, no waiting on a recipient existing.

    Bridges and grants are produced here but gate nothing: bridges are about
    reading old content and grants about other members reading the new
    content. Both are handed onward; neither is awaited.
    """
    required = sorted(frontier.loss_heads)
    parents = holdings.usable_heads()
    parent_secrets = holdings.reachable_secrets()
    authority_heads = sorted(frontier.heads)
    digest = acceptance.loss_projection_digest(frontier)

    if required:
        descriptor, secret, bridges = lifecycle.advance_state(
            author,
            domain_id,
            genesis_id,
            required,
            digest,
            parents,
            {p.state_id: parent_secrets[p.state_id] for p in parents},
            authority_heads,
        )
    elif holdings.descriptors:
        # Nothing is outstanding, yet no state was usable: this writer holds
        # no secret that reaches one. That is the unprovisioned member, which
        # is already broken for reading and is the capability-grant gap (§8),
        # not a contraction to cover. Minting here would strand an orphan
        # generation nobody can bridge to.
        raise StateAdvanceRequired(
            "no held secret reaches any state of this domain: this member has "
            "not been granted a capability yet"
        )
    else:
        # The domain has no generation at all — the first write founds one.
        descriptor, secret = state_mod.generate(
            author, genesis_id, domain_id, (), authority_heads, required, digest
        )
        bridges = ()

    grants = tuple(
        distribution.grant_current_head(
            author, domain_id, credential, descriptor, secret, authority_heads
        )
        for credential in recipient_credentials
    )
    return StateAdvance(descriptor, secret, tuple(bridges), grants)


def seal_revision(
    *,
    author,
    frontier,
    set_id: str,
    key: str,
    setting_id: str,
    payload,
    holdings: Holdings,
    ancestry,
    content_store,
    tier: str = AUDITED,
    policy_class=None,
    opener_seeds=None,
    recipient_credentials=(),
    body_suite_id: str = suites.BODY_SUITE_DEFAULT,
) -> SealedRevision:
    """Encrypt one setting revision and return the locator the row keeps.

    *holdings* is MUTATED when a generation is minted, so the next write in
    the same session finds it rather than minting a second one.

    Returns the sealed revision, including the :class:`StateAdvance` when one
    happened — its descriptor, bridges and grants are the caller's to hand to
    the broker. They are deliberately not published from here: publishing is
    not a precondition of the write, and a write that awaited it would have
    the outage the design spends §7.4 removing.
    """
    if tier not in TIERS:
        raise VaultError(f"unknown tier {tier!r}; this build provides {sorted(TIERS)}")
    genesis_id = frontier.genesis_id
    domain_id = organization_content_domain_id(genesis_id)
    object_id = object_id_for(genesis_id, set_id, key)
    revision_id = revision_id_for(genesis_id, setting_id)

    plaintext = canonical_json(payload)
    inner = None
    if tier == SECURED:
        if policy_class is None:
            raise VaultError(
                "a secured setting is sealed under a policy class; without one "
                "the storage state alone would open it, which is the audited tier"
            )
        inner = _SecuredBody(policy_class, opener_seeds or {}, object_id, genesis_id)

    header, body, advance = _create_with_inline_advance(
        author=author,
        domain_id=domain_id,
        genesis_id=genesis_id,
        frontier=frontier,
        holdings=holdings,
        ancestry=ancestry,
        object_id=object_id,
        revision_id=revision_id,
        body_suite_id=body_suite_id,
        plaintext=plaintext,
        inner=inner,
        recipient_credentials=recipient_credentials,
    )
    content_store.put_object(header, body)
    return SealedRevision(
        locator=build_locator(
            genesis_id=genesis_id,
            domain_id=domain_id,
            object_id=object_id,
            revision_id=revision_id,
            storage_state_id=header.storage_state_id,
            tier=tier,
            policy_class_id=policy_class.class_id if tier == SECURED else None,
            required_policy=policy_class.policy if tier == SECURED else None,
        ),
        header=header,
        body=body,
        advance=advance,
    )


class _SecuredBody:
    """The policy layer, applied beneath the storage state.

    The object body is not the payload: it is the payload's content key sealed
    under the policy class, together with the ciphertext that key opens. A
    member holding the storage state opens the object and reaches exactly
    that, which proves membership and nothing else (§9.2).

    The content key is bound to its object through the same body associated
    data the outer layer uses, so a sealed key lifted to another object,
    revision or generation does not open there. ``setting_name`` in the class
    wrap is the object id — the setting's derived identity in this domain, and
    the only name for it a reader holding just the locator can reconstruct.
    """

    def __init__(self, policy_class, opener_seeds, object_id, genesis_id):
        self.policy_class = policy_class
        self.opener_seeds = opener_seeds
        self.object_id = object_id
        self.genesis_id = genesis_id

    def wrap(self, plaintext: bytes, *, body_suite_id: str, **context) -> bytes:
        content_key = os.urandom(object_header.CEK_LEN)
        nonce = os.urandom(object_header.NONCE_LEN)
        ciphertext = object_header.seal_body(
            content_key,
            plaintext,
            body_suite_id=body_suite_id,
            body_nonce=nonce,
            **context,
        )
        sealed = seal_cek(
            self.policy_class,
            self.opener_seeds,
            content_key,
            genesis_id=self.genesis_id,
            setting_name=self.object_id,
            required_policy=self.policy_class.policy,
        )
        return canonical_json(
            {
                "v": _SECURED_ENVELOPE_VERSION,
                "sealed_cek": sealed,
                "body_suite_id": body_suite_id,
                "nonce": nonce.hex(),
                "ciphertext": ciphertext.hex(),
            }
        )


def _create_with_inline_advance(
    *,
    author,
    domain_id,
    genesis_id,
    frontier,
    holdings,
    ancestry,
    object_id,
    revision_id,
    body_suite_id,
    plaintext,
    inner,
    recipient_credentials,
):
    """``create_object`` under a covering generation, minting one if needed.

    The mint is retried into the SAME call rather than reported to the caller,
    because the design's whole claim about this path is that the safe state
    comes into existence inside the writer's own operation.
    """
    context = dict(
        genesis_id=genesis_id,
        domain_id=domain_id,
        object_id=object_id,
        revision_id=revision_id,
    )

    def _create():
        selected_states = holdings.usable_states()
        body_plaintext = plaintext
        if inner is not None:
            # The inner seal binds the storage state, so it can only be built
            # once that state is chosen — which is why the safety selection
            # runs first and the payload is wrapped against its result.
            state_id = lifecycle.select_safe_state(
                domain_id, frontier.loss_heads, selected_states.values(), ancestry
            ).state_id
            body_plaintext = inner.wrap(
                plaintext,
                body_suite_id=body_suite_id,
                storage_state_id=state_id,
                **context,
            )
        return objects.create_object(
            author,
            domain_id,
            body_plaintext,
            frontier,
            holdings.secrets,
            selected_states,
            ancestry=ancestry,
            bridges=list(holdings.bridges),
            descriptors=dict(holdings.descriptors),
            object_id=object_id,
            revision_id=revision_id,
            body_suite_id=body_suite_id,
        )

    try:
        header, body = _create()
        return header, body, None
    except StateAdvanceRequired:
        pass

    advance = _advance_in_line(
        author, domain_id, genesis_id, frontier, holdings, recipient_credentials
    )
    holdings.secrets[advance.descriptor.state_id] = advance.secret
    holdings.descriptors[advance.descriptor.state_id] = advance.descriptor
    holdings.bridges.extend(advance.bridges)
    header, body = _create()
    return header, body, advance


# ── the read path (contract §9) ────────────────────────────────────────────


@dataclass(frozen=True)
class SealedContentKey:
    """A ``secured`` revision, opened as far as membership alone reaches.

    Holding the storage state proves membership and yields exactly this: the
    content key still sealed under the policy class, and nothing else (§9.2).
    Opening THAT needs the class, which needs the human factor — so what a
    member without one holds is a key it cannot use, which is the nesting
    working rather than a check something could omit.

    Carries no ciphertext. A reader that gets this has learned the secret's
    release rule and no part of the secret.
    """

    policy_class_id: str
    required_policy: str
    sealed_cek: dict


def _open_object(locator, holdings: Holdings, content_store):
    """The locator's object, opened under the storage state.

    Every read of a vault revision goes through here, so the storage layer is
    entered at exactly one place and ``objects.read_object`` performs the whole
    sequence — content address, secret recovery through bridges, descriptor
    commitment, unwrap, open.
    """
    reference = parse_locator(locator)
    header, body = content_store.get_object(
        reference["object_id"], reference["revision_id"]
    )
    opened = objects.read_object(
        header, body, holdings.secrets, list(holdings.bridges), dict(holdings.descriptors)
    )
    return reference, header, opened


def open_revision_for_member(locator, *, holdings: Holdings, content_store):
    """What the storage state ALONE yields — a payload, or a sealed key.

    ``audited`` returns the payload; ``secured`` returns a
    :class:`SealedContentKey` rather than refusing, because a member with no
    human factor has still legitimately opened the object. What it found there
    is a sealed key, and reporting that is not a downgrade — handing back a
    value would be.

    :func:`open_revision` is the call for a reader that HAS the factor and
    wants the value; this one is for a resolver that must serve every reader
    and holds no factor for any of them.
    """
    reference, _header, opened = _open_object(locator, holdings, content_store)
    if reference["tier"] == AUDITED:
        return json.loads(opened)
    envelope = json.loads(opened)
    if envelope.get("v") != _SECURED_ENVELOPE_VERSION:
        raise VaultError(f"unsupported secured envelope version {envelope.get('v')!r}")
    return SealedContentKey(
        policy_class_id=reference["policy_class_id"],
        required_policy=reference["required_policy"],
        sealed_cek=envelope["sealed_cek"],
    )


def holds_a_descendant_of(state_id: str, holdings: Holdings) -> bool:
    """Whether this reader holds a generation DESCENDED from *state_id*.

    The storage layer refuses both cases the same way — no held secret reached
    the state — and they are different faults. A reader holding a descendant
    should have recovered the ancestor backward through parent bridges, so its
    failure says an edge is absent or unopenable. A reader holding nothing that
    descends from the state was simply never given a key that reaches it.

    Walks ``parent_state_ids`` in the public descriptors only: this is routing
    metadata, and answering it must not need a key.
    """
    descriptors = holdings.descriptors
    for held_id in sorted(holdings.secrets):
        held = descriptors.get(held_id)
        if held is None:
            continue
        seen = {held_id}
        frontier = list(held.parent_state_ids)
        while frontier:
            current = frontier.pop()
            if current == state_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            descriptor = descriptors.get(current)
            if descriptor is not None:
                frontier.extend(descriptor.parent_state_ids)
    return False


def open_revision(
    locator,
    *,
    holdings: Holdings,
    content_store,
    policy_class=None,
    opener_seeds=None,
):
    """Resolve a locator back to the setting's payload.

    Raises :class:`VaultError` when the locator itself is unusable and lets
    the storage layer's own refusals through untouched — a reader who cannot
    reach the key must see that, not an empty value indistinguishable from a
    setting that was never written.
    """
    reference, header, opened = _open_object(locator, holdings, content_store)
    if reference["tier"] == AUDITED:
        return json.loads(opened)

    if policy_class is None:
        raise VaultError(
            "this secret is secured: the storage state opened the object and "
            "yielded the wrapped content key, which opens only through its "
            "policy class"
        )
    if policy_class.class_id != reference["policy_class_id"]:
        raise VaultError(
            "the policy class offered is not the one this secret names"
        )
    envelope = json.loads(opened)
    if envelope.get("v") != _SECURED_ENVELOPE_VERSION:
        raise VaultError(f"unsupported secured envelope version {envelope.get('v')!r}")
    # Keep the CEK in mutable storage for the shortest practical lifetime.
    # ``open_cek`` necessarily returns one immutable bytes object from the
    # cryptography library; converting immediately lets that temporary fall out
    # of scope and gives this chokepoint a buffer it can reliably erase.
    content_key = bytearray(
        open_cek(
            policy_class,
            opener_seeds or {},
            envelope["sealed_cek"],
            genesis_id=reference["genesis_id"],
            setting_name=reference["object_id"],
            required_policy=reference["required_policy"],
        )
    )
    try:
        suites.require_suite(envelope["body_suite_id"], suites.BODY_SUITES)
        inner_header = _replace_body(header, envelope)
        plaintext = object_header.open_body(
            inner_header, content_key, bytes.fromhex(envelope["ciphertext"])
        )
    finally:
        content_key[:] = b"\x00" * len(content_key)
    return json.loads(plaintext)


def _replace_body(header, envelope):
    """The header as it applies to the INNER ciphertext.

    ``open_body`` reads the context it authenticates from a header, and the
    inner layer was sealed under the same context with its own nonce and its
    own content address. Rather than restate that context by hand — where a
    field could be forgotten and the binding silently weakened — the outer
    header is copied with exactly the two fields that differ replaced.
    """
    ciphertext = bytes.fromhex(envelope["ciphertext"])
    return dataclass_replace(
        header,
        body_suite_id=envelope["body_suite_id"],
        body_nonce=base64.b64encode(bytes.fromhex(envelope["nonce"])).decode("ascii"),
        ciphertext_hash=hashlib.sha256(ciphertext).hexdigest(),
    )
