"""Content object create and read — the CRUD path over the storage engine.

**Create** (contract §10): verify write authority via the single §4
membership resolution; select a safe state covering the frontier's
required contractions (or recover the selected state's secret from a
held DESCENDANT through bridges) — halting with ``StateAdvanceRequired``
only when neither path yields a covering secret; then encrypt the body
exactly once under a fresh content-encryption key wrapped beneath the
state secret. Durability is acknowledged, not gated (ruling D): creation
proceeds under any safe state regardless of receipt counts — the object
is stored, served, and replicated immediately, and commit status is the
distribution module's report, surfaced by callers. The write halts are
the confidentiality gates alone.

**Read** (contract §9): confirm the content address, recover the
referenced state secret — directly or backward through parent bridges
from any held descendant, each hop commitment-checked — verify it
against the descriptor commitment, unwrap the key, open the body. A
holder that reaches the referenced state reads; one holding only
ancestors does not (Invariant 4).

Pure over caller-supplied stores; composes the state, bridge,
object-header, acceptance, and lifecycle modules; no new cryptographic
construction.
"""

from __future__ import annotations

import hashlib
import os

from tools.network.ledger.projections import organization_content_domain_id

from . import bridge as bridge_mod
from . import object_header
from . import state as state_mod
from .acceptance import AuthorityError, DomainError, resolve_member_key
from .errors import StorageError
from .lifecycle import StateAdvanceRequired, select_safe_state


class BodyHashError(StorageError):
    """The body ciphertext does not match the header's content address."""


class StateUnreachableError(StorageError):
    """No held secret reaches the referenced state through bridges."""


def _reach_secret(state_id: str, held_secrets, bridges, descriptors):
    """The state's secret, held directly or recovered backward from any
    held descendant (every hop commitment-verified in the bridge module);
    None when no held secret reaches it."""
    direct = held_secrets.get(state_id)
    if direct is not None:
        return direct
    for held_id in sorted(held_secrets):
        recovered = bridge_mod.recover_ancestors(
            held_id, held_secrets[held_id], bridges, descriptors
        )
        if state_id in recovered:
            return recovered[state_id]
    return None


def create_object(
    author,
    domain_id: str,
    plaintext: bytes,
    frontier,
    held_secrets,
    available_states,
    *,
    ancestry,
    bridges=(),
    descriptors=None,
    object_id: str,
    revision_id: str,
    body_suite_id: str,
) -> tuple:
    """Encrypt *plaintext* once under a safe state at *frontier*.

    Returns ``(header, body_ciphertext)`` without persisting. *frontier*
    is the FoldState at the writer's cited heads; *ancestry* the
    causal-closure seam the safety predicate runs over.
    """
    if domain_id != organization_content_domain_id(frontier.genesis_id):
        raise DomainError("create names a different storage domain")
    if resolve_member_key(frontier, author.public_hex) is None:
        raise AuthorityError("author does not resolve to a domain member")
    selected = select_safe_state(
        domain_id, frontier.loss_heads, available_states.values(), ancestry
    )
    state_secret = _reach_secret(
        selected.state_id, held_secrets, bridges, descriptors or {}
    )
    if state_secret is None:
        raise StateAdvanceRequired(
            "no held secret reaches the selected safe state"
        )
    # A held secret must be the selected state's actual secret.
    state_mod.verify_secret_commitment(selected, state_secret)

    cek = os.urandom(object_header.CEK_LEN)
    body_nonce = os.urandom(object_header.NONCE_LEN)
    wrap_nonce = os.urandom(object_header.NONCE_LEN)
    context = dict(
        genesis_id=frontier.genesis_id,
        domain_id=domain_id,
        object_id=object_id,
        revision_id=revision_id,
        storage_state_id=selected.state_id,
    )
    body_ciphertext = object_header.seal_body(
        cek, plaintext, body_suite_id=body_suite_id, body_nonce=body_nonce, **context
    )
    header = object_header.build(
        author,
        state_secret,
        cek,
        writer_authority_heads=list(frontier.heads),
        body_suite_id=body_suite_id,
        body_nonce=body_nonce,
        wrap_nonce=wrap_nonce,
        ciphertext_hash=hashlib.sha256(body_ciphertext).hexdigest(),
        **context,
    )
    return header, body_ciphertext


def read_object(header, body_ciphertext, held_secrets, bridges, descriptors) -> bytes:
    """Open a content body for a holder that reaches its state.

    Content address first (before any unwrap), then secret recovery,
    descriptor-commitment verification, unwrap, open — every failure a
    distinct fail-closed error, no path returning plaintext early.
    """
    if not isinstance(body_ciphertext, (bytes, bytearray)):
        raise BodyHashError("body ciphertext must be bytes")
    blob = bytes(body_ciphertext)
    if hashlib.sha256(blob).hexdigest() != header.ciphertext_hash:
        raise BodyHashError("body does not match the header content address")
    secret = _reach_secret(header.storage_state_id, held_secrets, bridges, descriptors)
    if secret is None:
        raise StateUnreachableError(
            "no held secret reaches the object's storage state"
        )
    descriptor = descriptors.get(header.storage_state_id)
    if descriptor is None:
        raise StateUnreachableError("no descriptor for the object's storage state")
    state_mod.verify_secret_commitment(descriptor, secret)
    cek = object_header.unwrap_cek(secret, header)
    return object_header.open_body(header, cek, blob)


def count_object(counters, header) -> int:
    """Per-state object counter (CONTINUITY-SURFACE INDEX REQUIREMENTS).

    Every path admitting an accepted object to a store calls this in the
    same admission step, so continuity warnings read one count per state
    and never scan object rows. Returns the new count.
    """
    counters[header.storage_state_id] = counters.get(header.storage_state_id, 0) + 1
    return counters[header.storage_state_id]
