"""Membership commitment — the fold's membership as two Merkle roots.

Implements the commitment layer of the committed-membership design
(graph://da0dd9fb-e75, bead auto-i24yr): ``members_root`` over every live
member's CURRENT persona public key, and ``checkpointers_root`` over the
subset whose folded authority covers :data:`CHECKPOINT_SCOPE`, plus the
signed checkpoint record that carries them and the inclusion proofs the
registry verifies.

Tree shape: leaves are sorted by persona public key and the tree is padded
to the next power of two with a domain-separated padding leaf, so every
inclusion proof has exactly ``ceil(log2 n)`` siblings and verification
derives left/right from the index bits alone — no tree size travels on the
wire, preserving the depth-only size leak the design's privacy section
promises. (This refines the decision note's "RFC 6962 odd-node promotion"
wording: strict RFC 6962 audit paths need the exact tree size in every
proof, which would disclose the exact member count; recorded as a comment
on graph://da0dd9fb-e75.)

Determinism is the contract: two folds of the same ledger must produce
byte-identical roots. Everything here is pure computation over hex strings
and canonical JSON; nothing reads storage or the clock.

Checkpoint chain rules (validated by :func:`validate_checkpoint`):

- A ROOT-SIGNED record (seed at seq 0, reset at any seq) carries no proof
  fields, verifies against the org's bound root key, and re-anchors the
  chain: its ``prev`` is the ledger ``genesis_id``, not a checkpoint hash.
- A MEMBER-SIGNED record advances ``seq`` by exactly one, hash-links
  ``prev`` to the previous signed record, and embeds the signer's own
  inclusion proof under the PREVIOUS record's ``checkpointers_root`` —
  the induction that lets the registry verify membership without ever
  folding the ledger.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from tools.network.idkit import KeyPair, canonical_json, verify_signature
from tools.network.idkit.errors import IdkitError

LEAF_DOMAIN = b"autonomy.network.membership.leaf.v1\n"
NODE_DOMAIN = b"autonomy.network.membership.node.v1\n"
CHECKPOINT_DOMAIN = b"autonomy.network.membership.checkpoint.v1\n"

#: The fold scope whose holders may sign membership checkpoints. An owner's
#: ``*`` covers it (scopes.set_covers), so the founder is sole member and
#: sole checkpointer at genesis with no special casing.
CHECKPOINT_SCOPE = "membership:checkpoint"
#: The ledger event types a fold's member set (and so members_root and
#: checkpointers_root) can depend on: claims and rekeys (leaves), revokes
#: (kills, and inviter-demotion races through delegate revocation),
#: role.revoke (inviter-demotion races) and key.rotate (root-rotation races
#: for root-issued invites) -- fold.py:_claim_alive, _concurrent_revocations,
#: _rotation_race. Appending any other type never changes either root
#: (property test in the ledger suite). This ONE set is both where a
#: checkpoint is published at admission (OrgAdmission.tla P2) and where a
#: prover folds its own ledger for candidate roots
#: (OrgAdmissionBundleBound.tla ProveOwnFold), so every checkpointed root is
#: a candidate (reviewer condition on 13284c4c).
MEMBER_SET_EVENT_TYPES = frozenset(
    {"member.claim", "member.rekey", "revoke", "role.revoke", "key.rotate"}
)

#: Padding leaf for the perfect tree. Its preimage is domain plus a tag,
#: never 32 key bytes, so it cannot collide with any real leaf.
_PADDING_LEAF = hashlib.sha256(LEAF_DOMAIN + b"padding.v1").digest()

#: Root of a committed EMPTY set. Never produced by a valid fold (an org
#: has at least its founder) but total by construction.
EMPTY_ROOT = hashlib.sha256(NODE_DOMAIN + b"empty.v1").hexdigest()

#: Proof depth bound: 2**64 leaves is unreachable; refuse absurd proofs
#: before doing work proportional to them.
MAX_PROOF_DEPTH = 64

_HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_HEX128_RE = re.compile(r"\A[0-9a-f]{128}\Z")

_COMMON_FIELDS = frozenset(
    {"v", "org", "seq", "prev", "ledger_head", "members_root",
     "checkpointers_root", "ts", "signer", "sig"}
)
_MEMBER_FIELDS = _COMMON_FIELDS | {"proof", "proof_index"}
#: The delegate-signed form (DelegateCheckpoint.spthy; OrgAdmission.tla P2):
#: ``signer`` is a hot delegate key, ``grant`` is the persona-signed ledger
#: ``delegate`` event (with the child's proof of possession) that attributes
#: it, ``genesis_id`` is what that proof was made over, and ``proof`` places
#: the GRANTING persona under the previous checkpointers root.
_DELEGATE_FIELDS = _MEMBER_FIELDS | {"grant", "genesis_id"}
CHECKPOINT_VERSION = 1
#: The two refusals that mean "the verifier's stored record is not the one
#: this record chains from": a publisher that sees one re-reads the
#: registry and re-assembles on its record (membership_checkpoint F3).
REFUSAL_SEQ = "checkpoint seq must advance the previous record by exactly one"
REFUSAL_PREV = "checkpoint prev does not hash-link to the previous record"
CHAIN_REFUSALS = (REFUSAL_SEQ, REFUSAL_PREV)


class MembershipCommitmentError(Exception):
    """A malformed or unverifiable commitment artifact. The message names
    the first rule that failed; callers surface it verbatim."""


# -- trees ---------------------------------------------------------------------


def _require_pub(value: object, what: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.match(value) is None:
        raise MembershipCommitmentError(
            f"{what} must be a 64-char lowercase hex public key")
    return value


def leaf_hash(persona_pub_hex: str) -> bytes:
    """SHA256(LEAF_DOMAIN || raw 32 key bytes)."""
    pub = _require_pub(persona_pub_hex, "leaf persona pub")
    return hashlib.sha256(LEAF_DOMAIN + bytes.fromhex(pub)).digest()


def _node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(NODE_DOMAIN + left + right).digest()


def _sorted_pubs(pubs: Iterable[str]) -> List[str]:
    out = sorted({_require_pub(p, "persona pub") for p in pubs})
    return out


def _padded_leaves(pubs: Sequence[str]) -> List[bytes]:
    leaves = [leaf_hash(p) for p in pubs]
    size = 1
    while size < len(leaves):
        size *= 2
    leaves.extend([_PADDING_LEAF] * (size - len(leaves)))
    return leaves


def compute_root(pubs: Iterable[str]) -> str:
    """The 64-hex Merkle root committing *pubs* (order-insensitive input;
    the tree is over the sorted, deduplicated set)."""
    ordered = _sorted_pubs(pubs)
    if not ordered:
        return EMPTY_ROOT
    level = _padded_leaves(ordered)
    while len(level) > 1:
        level = [_node_hash(level[i], level[i + 1])
                 for i in range(0, len(level), 2)]
    return level[0].hex()


def inclusion_proof(pubs: Iterable[str], persona_pub: str) -> Tuple[int, List[str]]:
    """(index, sibling path bottom-up, hex) for *persona_pub* under the tree
    over *pubs*. Raises when the persona is not in the set."""
    ordered = _sorted_pubs(pubs)
    pub = _require_pub(persona_pub, "persona pub")
    try:
        index = ordered.index(pub)
    except ValueError:
        raise MembershipCommitmentError(
            "persona is not in the committed set") from None
    level = _padded_leaves(ordered)
    path: List[str] = []
    i = index
    while len(level) > 1:
        sibling = level[i ^ 1]
        path.append(sibling.hex())
        level = [_node_hash(level[j], level[j + 1])
                 for j in range(0, len(level), 2)]
        i //= 2
    return index, path


def verify_inclusion(root_hex: str, persona_pub: str, index: int,
                     path: Sequence[str]) -> None:
    """Verify *persona_pub* is committed at *index* under *root_hex*.

    Directions come from the index bits (the tree is perfect by padding).
    Raises :class:`MembershipCommitmentError` on any failure; returns None
    on success — the same contract as idkit's ``verify_signature``.
    """
    if not isinstance(root_hex, str) or _HEX64_RE.match(root_hex) is None:
        raise MembershipCommitmentError("root must be 64 lowercase hex chars")
    if type(index) is not int or index < 0:
        raise MembershipCommitmentError("proof index must be a non-negative int")
    if not isinstance(path, (list, tuple)) or len(path) > MAX_PROOF_DEPTH:
        raise MembershipCommitmentError(
            f"proof path must be a list of at most {MAX_PROOF_DEPTH} siblings")
    if index >> len(path):
        raise MembershipCommitmentError("proof index exceeds the tree depth")
    node = leaf_hash(persona_pub)
    i = index
    for sibling_hex in path:
        if not isinstance(sibling_hex, str) or _HEX64_RE.match(sibling_hex) is None:
            raise MembershipCommitmentError(
                "proof siblings must be 64 lowercase hex chars")
        sibling = bytes.fromhex(sibling_hex)
        node = _node_hash(node, sibling) if i % 2 == 0 else _node_hash(sibling, node)
        i //= 2
    if node.hex() != root_hex:
        raise MembershipCommitmentError(
            "inclusion proof does not verify against the committed root")


# -- fold projections ----------------------------------------------------------


def member_pubs(state) -> Tuple[str, ...]:
    """Every live member's CURRENT persona public key, sorted.

    *state* is a ``fold.FoldState`` (duck-typed: ``members`` mapping of
    persona id → view with ``current_key``). Leaves are current keys, so
    ``member.rekey`` swaps exactly one leaf.
    """
    return tuple(sorted(m.current_key for m in state.members.values()))


def checkpointer_pubs(state) -> Tuple[str, ...]:
    """The member subset whose folded authority covers
    :data:`CHECKPOINT_SCOPE` (pattern coverage, so ``*`` holders qualify)."""
    return tuple(sorted(
        m.current_key for m in state.members.values()
        if state.holds(m.current_key, CHECKPOINT_SCOPE)
    ))


def members_root(state) -> str:
    return compute_root(member_pubs(state))


def checkpointers_root(state) -> str:
    return compute_root(checkpointer_pubs(state))


# -- checkpoint records --------------------------------------------------------


def checkpoint_hash(record: Dict) -> str:
    """SHA256 over the full signed record's canonical bytes — the value the
    next record's ``prev`` carries."""
    return hashlib.sha256(canonical_json(record)).hexdigest()


def _signing_input(record_sans_sig: Dict) -> bytes:
    return CHECKPOINT_DOMAIN + canonical_json(record_sans_sig)


def _base_record(*, org: str, seq: int, prev: str, ledger_head: str,
                 members_root_hex: str, checkpointers_root_hex: str,
                 ts: int, signer_pub: str) -> Dict:
    if not isinstance(org, str) or not org or len(org) > 128:
        raise MembershipCommitmentError("org must be a non-empty string")
    if type(seq) is not int or seq < 0:
        raise MembershipCommitmentError("seq must be a non-negative int")
    if type(ts) is not int or ts < 0:
        raise MembershipCommitmentError("ts must be a non-negative unix-seconds int")
    for what, value in (("prev", prev), ("ledger_head", ledger_head),
                        ("members_root", members_root_hex),
                        ("checkpointers_root", checkpointers_root_hex)):
        if not isinstance(value, str) or _HEX64_RE.match(value) is None:
            raise MembershipCommitmentError(f"{what} must be 64 lowercase hex chars")
    return {
        "v": CHECKPOINT_VERSION,
        "org": org,
        "seq": seq,
        "prev": prev,
        "ledger_head": ledger_head,
        "members_root": members_root_hex,
        "checkpointers_root": checkpointers_root_hex,
        "ts": ts,
        "signer": _require_pub(signer_pub, "signer"),
    }


def build_checkpoint(*, org: str, seq: int, prev: str, ledger_head: str,
                     members_root_hex: str, checkpointers_root_hex: str,
                     ts: int, signer: KeyPair,
                     prev_checkpointer_pubs: Iterable[str]) -> Dict:
    """A member-signed checkpoint. The signer must be in
    *prev_checkpointer_pubs* — the checkpointer set of the PREVIOUS record —
    because that is the root the verifier holds when this record arrives."""
    record = _base_record(
        org=org, seq=seq, prev=prev, ledger_head=ledger_head,
        members_root_hex=members_root_hex,
        checkpointers_root_hex=checkpointers_root_hex,
        ts=ts, signer_pub=signer.public_hex,
    )
    if seq < 1:
        raise MembershipCommitmentError(
            "member-signed checkpoints start at seq 1; seq 0 is the root-signed seed")
    index, path = inclusion_proof(prev_checkpointer_pubs, signer.public_hex)
    record["proof"] = path
    record["proof_index"] = index
    record["sig"] = signer.sign_hex(_signing_input(record))
    return record


def build_delegate_checkpoint(*, org: str, seq: int, prev: str, ledger_head: str,
                              members_root_hex: str, checkpointers_root_hex: str,
                              ts: int, delegate: KeyPair, grant_wire: str,
                              genesis_id: str,
                              prev_checkpointer_pubs: Iterable[str]) -> Dict:
    """A checkpoint signed by a hot delegate holding CHECKPOINT_SCOPE
    (the checkpoint published at admission with no persona present). The
    record carries the delegate's persona-signed grant so a verifier without
    the ledger attributes it: the GRANTING persona must be in
    *prev_checkpointer_pubs*, and the proof places that persona."""
    from tools.network.ledger.events import Event

    grant = Event.from_json(grant_wire)
    if grant.type != "delegate" or grant.payload.get("child_pub") != delegate.public_hex:
        raise MembershipCommitmentError("grant does not delegate to this signing key")
    record = _base_record(
        org=org, seq=seq, prev=prev, ledger_head=ledger_head,
        members_root_hex=members_root_hex,
        checkpointers_root_hex=checkpointers_root_hex,
        ts=ts, signer_pub=delegate.public_hex,
    )
    if seq < 1:
        raise MembershipCommitmentError(
            "delegate-signed checkpoints start at seq 1; seq 0 is the root-signed seed")
    index, path = inclusion_proof(prev_checkpointer_pubs, grant.author_key)
    record["proof"] = path
    record["proof_index"] = index
    record["grant"] = grant_wire if isinstance(grant_wire, str) else grant_wire.decode("utf-8")
    record["genesis_id"] = _require_hex64(genesis_id, "genesis_id")
    record["sig"] = delegate.sign_hex(_signing_input(record))
    return record


def _require_hex64(value: object, what: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.match(value) is None:
        raise MembershipCommitmentError(f"{what} must be 64 lowercase hex chars")
    return value


def chain_record_for(retained: Dict) -> Optional[Dict]:
    """The SIGNED record whose hash the next checkpoint's ``prev`` must
    carry, for a retained entry: the entry itself when it is a signed
    record (published here, or root-signed and verified), else the
    ``chain_record`` a fold adoption kept beside the authenticated tuple
    (the registry's or a bundle's signed bytes, used for chaining only and
    never for trust). None when the entry holds no signed bytes."""
    if not isinstance(retained, dict):
        return None
    if isinstance(retained.get("sig"), str) and isinstance(retained.get("signer"), str):
        return retained
    chain = retained.get("chain_record")
    if isinstance(chain, dict) and isinstance(chain.get("sig"), str):
        return chain
    return None


def checkpoint_signer_persona(record: Dict) -> str:
    """The persona a signed record is attributed to: the signer for the root
    and member forms; the grant's author for the delegate form."""
    if "grant" in record:
        from tools.network.ledger.events import Event
        return str(Event.from_json(record["grant"]).author_key)
    return str(record["signer"])


def _attribute_delegate(record: Dict, *, now: Optional[int]) -> str:
    """Verify the delegate form's attribution chain (persona -> grant ->
    delegate, with the delegate's proof of possession; DelegateCheckpoint.spthy
    Accept_Checkpoint) and its validity against the VERIFIER's clock; return
    the attributed persona. *now* is unix seconds; None skips the clock
    checks (history replay), never an acceptance."""
    from tools.network import clock
    from tools.network.idkit.errors import IdkitError as _IdkitError
    from tools.network.ledger.events import Event, delegate_proof_input

    try:
        grant = Event.from_json(record["grant"])
        grant.verify_sig()
    except Exception as exc:  # malformed or unsigned grant: one refusal channel
        raise MembershipCommitmentError(f"checkpoint grant does not verify: {exc}") from exc
    p = grant.payload
    if grant.type != "delegate":
        raise MembershipCommitmentError("checkpoint grant is not a delegate event")
    if p.get("child_pub") != record["signer"]:
        raise MembershipCommitmentError("checkpoint grant does not name the signer")
    scope = p.get("scope")
    if not isinstance(scope, list) or CHECKPOINT_SCOPE not in scope:
        raise MembershipCommitmentError("checkpoint grant does not carry the checkpoint scope")
    if p.get("can_redelegate") is not False:
        raise MembershipCommitmentError("checkpoint grant must not be redelegable")
    ttl = p.get("ttl")
    if type(ttl) is not int or ttl <= 0:
        raise MembershipCommitmentError("checkpoint grant must carry a positive ttl")
    try:
        verify_signature(
            p["child_pub"], p["proof"],
            delegate_proof_input(
                record["genesis_id"], grant.author_key, p["child_pub"], scope,
                can_redelegate=False, ttl=ttl, grant_nonce=p["grant_nonce"],
            ),
        )
    except (_IdkitError, KeyError, TypeError) as exc:
        raise MembershipCommitmentError(
            f"checkpoint grant proof of possession does not verify: {exc}") from exc
    if now is not None:
        # The record's ts is the delegate's own word; judge by our clock.
        if abs(int(record["ts"]) - int(now)) > clock.MAX_CLOCK_SKEW:
            raise MembershipCommitmentError(
                f"delegate-signed checkpoint ts is outside the verifier's "
                f"±{clock.MAX_CLOCK_SKEW}s window")
        if int(now) * 1000 >= int(grant.hlc.ts) + ttl:
            raise MembershipCommitmentError("checkpoint grant has expired at the verifier")
    return str(grant.author_key)


def build_root_checkpoint(*, org: str, seq: int, genesis_id: str,
                          ledger_head: str, members_root_hex: str,
                          checkpointers_root_hex: str, ts: int,
                          root: KeyPair) -> Dict:
    """A root-signed seed (seq 0) or reset (any seq). Carries no proof and
    re-anchors the chain: ``prev`` is the ledger *genesis_id*."""
    record = _base_record(
        org=org, seq=seq, prev=genesis_id, ledger_head=ledger_head,
        members_root_hex=members_root_hex,
        checkpointers_root_hex=checkpointers_root_hex,
        ts=ts, signer_pub=root.public_hex,
    )
    record["sig"] = root.sign_hex(_signing_input(record))
    return record


def validate_checkpoint(record: object, *, root_pub: str,
                        prev_record: Optional[Dict] = None,
                        now: Optional[int] = None) -> None:
    """Validate one received checkpoint record.

    ROOT-SIGNED (``signer == root_pub``): no proof fields allowed; the
    signature alone authorizes, at any seq (seed and reset).

    MEMBER-SIGNED: *prev_record* is REQUIRED — seq must be exactly
    ``prev.seq + 1``, ``prev`` must hash-link to it, ``org`` must match,
    and the embedded proof must place the signer under the previous
    record's ``checkpointers_root``.

    DELEGATE-SIGNED (the record carries ``grant``): as member-signed, with
    the signer a hot delegate attributed through its carried, persona-signed
    grant (signature, proof of possession, checkpoint scope) to the GRANTING
    persona, which the proof must place under the previous checkpointers
    root; the record's ts and the grant's ttl are judged by *now*, the
    verifier's own clock (unix seconds), which a submission-time verifier
    MUST pass (None only for history replay).

    Raises :class:`MembershipCommitmentError` naming the first failed rule;
    returns None on success. Signature verification failures surface as
    the same error type so callers have one refusal channel.
    """
    if not isinstance(record, dict):
        raise MembershipCommitmentError("checkpoint must be a JSON object")
    if record.get("v") != CHECKPOINT_VERSION:
        raise MembershipCommitmentError("unsupported checkpoint version")
    signer = record.get("signer")
    _require_pub(signer, "signer")
    root_signed = signer == root_pub
    delegate_signed = not root_signed and "grant" in record
    expected = _COMMON_FIELDS if root_signed else (
        _DELEGATE_FIELDS if delegate_signed else _MEMBER_FIELDS)
    if set(record) != expected:
        missing = sorted(expected - set(record))
        unknown = sorted(set(record) - expected)
        raise MembershipCommitmentError(
            "checkpoint fields do not match its form"
            + (f" (missing {missing})" if missing else "")
            + (f" (unknown {unknown})" if unknown else ""))
    sig = record.get("sig")
    if not isinstance(sig, str) or _HEX128_RE.match(sig) is None:
        raise MembershipCommitmentError("sig must be 128 lowercase hex chars")
    # Re-run the structural checks build enforces, on the received bytes.
    _base_record(
        org=record["org"], seq=record["seq"], prev=record["prev"],
        ledger_head=record["ledger_head"],
        members_root_hex=record["members_root"],
        checkpointers_root_hex=record["checkpointers_root"],
        ts=record["ts"], signer_pub=signer,
    )

    if not root_signed:
        if prev_record is None:
            raise MembershipCommitmentError(
                "member-signed checkpoint requires the previous record")
        if record["seq"] != prev_record["seq"] + 1:
            raise MembershipCommitmentError(REFUSAL_SEQ)
        if record["org"] != prev_record["org"]:
            raise MembershipCommitmentError(
                "checkpoint org does not match the previous record")
        if record["prev"] != checkpoint_hash(prev_record):
            raise MembershipCommitmentError(REFUSAL_PREV)
        attributed = signer
        if delegate_signed:
            _require_hex64(record.get("genesis_id"), "genesis_id")
            if not isinstance(record.get("grant"), str):
                raise MembershipCommitmentError("checkpoint grant must be the event wire")
            attributed = _attribute_delegate(record, now=now)
        verify_inclusion(prev_record["checkpointers_root"], attributed,
                         record["proof_index"], record["proof"])

    unsigned = {k: v for k, v in record.items() if k != "sig"}
    try:
        verify_signature(signer, sig, _signing_input(unsigned))
    except IdkitError as exc:
        raise MembershipCommitmentError(
            f"checkpoint signature does not verify: {exc}") from exc
