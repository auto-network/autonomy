"""Ledger events: the authority vocabulary, signing, and wire form.

Every event is ``{v, author_key, parents, hlc, payload, sig}``:

- ``author_key`` — hex Ed25519 public key of the signer.
- ``parents`` — sorted, duplicate-free list of event ids (the author's
  seen-heads); empty only for ``genesis``.
- ``hlc`` — ``[ts_ms, count]`` ordering hint (see :mod:`.hlc`).
- ``payload`` — ``{"type": <kind>, ...}`` from the closed vocabulary below.
- ``sig`` — Ed25519 over ``EVENT_DOMAIN || canonical_json(dict minus sig)``.

The event id is the SHA-256 hex of the canonical wire bytes (signature
included), so ids commit to the signature the way git commit ids do.

**L8 lives here**: ``validate_payload`` accepts *only* the authority
vocabulary. Anything else — content views, reads, ad-hoc kinds — raises
:class:`~.errors.SchemaError` before the event can exist at all.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from tools.network.idkit import KeyPair, canonical_json, verify_signature
from tools.network.idkit.errors import (
    MalformedError as _IdkitMalformed,
    SignatureError as _IdkitSignatureError,
)
from tools.network.idkit.keys import PUBLIC_KEY_HEX_LEN, SIGNATURE_HEX_LEN, _decode_hex

from .errors import MalformedEventError, SchemaError, SignatureError
from .hlc import HLC
from .scopes import validate_scope, validate_scope_list

EVENT_DOMAIN = b"autonomy.ledger.event.v1\n"
APPROVAL_DOMAIN = b"autonomy.ledger.approval.v1\n"
ROTATE_DOMAIN = b"autonomy.ledger.rotate-continuity.v1\n"
#: Frozen, byte-identical to storagekit.credentials.CREDENTIAL_DOMAIN —
#: the fold verifies an embedded kem_credential with idkit only, and a
#: cross-package fidelity test keeps the two constants from drifting.
KEM_CREDENTIAL_DOMAIN = b"autonomy.storage.persona-kem-credential.v1\n"
EVENT_VERSION = 1

EVENT_HASH_HEX_LEN = 64
MAX_PARENTS = 32
MAX_EVENT_BYTES = 16_384
MAX_APPROVALS = 16
MAX_STR = 256
MAX_ROLE_NAME = 64

CLAIM_REQUIRES = ("admin-ack", "self", "sponsor")

_ROLE_NAME_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-._")

_EVENT_FIELDS = frozenset({"v", "author_key", "parents", "hlc", "payload", "sig"})


def _require_str(value: object, what: str, max_len: int = MAX_STR) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise SchemaError(f"{what} must be a non-empty string of at most {max_len} chars")
    return value


def _require_key(value: object, what: str) -> str:
    _require_str(value, what, max_len=PUBLIC_KEY_HEX_LEN)
    try:
        _decode_hex(value, PUBLIC_KEY_HEX_LEN, what)
    except _IdkitMalformed as exc:
        raise SchemaError(str(exc)) from None
    return value


def _require_hash(value: object, what: str) -> str:
    _require_str(value, what, max_len=EVENT_HASH_HEX_LEN)
    try:
        _decode_hex(value, EVENT_HASH_HEX_LEN, what)
    except _IdkitMalformed as exc:
        raise SchemaError(str(exc)) from None
    return value


def require_hash_list(
    value: object,
    what: str,
    max_len: int,
    *,
    allow_empty: bool = True,
    exc: type = SchemaError,
) -> list:
    """A sorted, duplicate-free list of event ids — the one hash-list
    grammar shared by sync messages and bundle manifests (F3)."""
    if not isinstance(value, list) or len(value) > max_len:
        raise exc(f"{what} must be a list of at most {max_len} event ids")
    if not value and not allow_empty:
        raise exc(f"{what} must not be empty")
    for entry in value:
        try:
            _require_hash(entry, f"{what} entry")
        except SchemaError as e:
            raise exc(str(e)) from None
    if value != sorted(set(value)):
        raise exc(f"{what} must be sorted and free of duplicates")
    return value


def _require_ts(value: object, what: str) -> int:
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise SchemaError(f"{what} must be an integer unix-ms timestamp in [0, 2**63)")
    return value


def _require_role_name(value: object, what: str = "role") -> str:
    _require_str(value, what, max_len=MAX_ROLE_NAME)
    if not set(value) <= _ROLE_NAME_OK:
        raise SchemaError(f"{what} must use only [a-z0-9-._]")
    return value


def _require_fields(payload: dict, kind: str, required: frozenset, optional: frozenset = frozenset()):
    present = set(payload) - {"type"}
    unknown = present - required - optional
    if unknown:
        raise SchemaError(f"{kind} payload carries unknown fields: {sorted(unknown)}")
    missing = required - present
    if missing:
        raise SchemaError(f"{kind} payload is missing fields: {sorted(missing)}")


def _require_approvals(value: object, what: str = "approvals") -> list:
    if not isinstance(value, list) or len(value) > MAX_APPROVALS:
        raise SchemaError(f"{what} must be a list of at most {MAX_APPROVALS} entries")
    keys = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"key", "sig"}:
            raise SchemaError(f"{what} entries must be objects with exactly {{key, sig}}")
        _require_key(entry["key"], f"{what}.key")
        _require_str(entry["sig"], f"{what}.sig", max_len=SIGNATURE_HEX_LEN)
        try:
            _decode_hex(entry["sig"], SIGNATURE_HEX_LEN, f"{what}.sig")
        except _IdkitMalformed as exc:
            raise SchemaError(str(exc)) from None
        keys.append(entry["key"])
    if keys != sorted(set(keys)):
        raise SchemaError(f"{what} must be sorted by key and free of duplicate keys")
    return value


#: Contract §5 credential record: exactly these nine fields.
_KEM_CREDENTIAL_FIELDS = frozenset(
    {
        "version",
        "suite_id",
        "genesis_id",
        "persona",
        "kem_public_key",
        "kem_key_id",
        "authority_heads",
        "created_hlc",
        "signature",
    }
)
MAX_KEM_CREDENTIAL_BYTES = 1024
MAX_KEM_CREDENTIAL_HEADS = 16


def _require_kem_credential(value: object, persona_pub: str) -> None:
    """Structural §5 checks on an embedded persona KEM credential.

    Deep acceptance (fold verification of signature/identifier/genesis)
    is the fold handler's; full currency/roster checks live in
    storagekit. Any violation raises :class:`SchemaError` (L8).
    """
    if not isinstance(value, dict) or set(value) != _KEM_CREDENTIAL_FIELDS:
        raise SchemaError(
            "kem_credential must carry exactly the contract §5 fields: "
            f"{sorted(_KEM_CREDENTIAL_FIELDS)}"
        )
    if type(value["version"]) is not int or value["version"] < 1:
        raise SchemaError("kem_credential.version must be a positive integer")
    # The seal-suite identifier is idkit's integer wire tag; exact-type,
    # bool-rejecting (the require_suite discipline).
    if type(value["suite_id"]) is not int or not 0 <= value["suite_id"] <= 255:
        raise SchemaError("kem_credential.suite_id must be an integer wire tag in [0, 255]")
    _require_hash(value["genesis_id"], "kem_credential.genesis_id")
    _require_key(value["persona"], "kem_credential.persona")
    _require_key(value["kem_public_key"], "kem_credential.kem_public_key")
    _require_hash(value["kem_key_id"], "kem_credential.kem_key_id")
    require_hash_list(
        value["authority_heads"],
        "kem_credential.authority_heads",
        MAX_KEM_CREDENTIAL_HEADS,
        allow_empty=False,
    )
    hlc = value["created_hlc"]
    if (
        not isinstance(hlc, list)
        or len(hlc) != 2
        or any(type(v) is not int or v < 0 for v in hlc)
    ):
        raise SchemaError("kem_credential.created_hlc must be [ts_ms, count], non-negative ints")
    _require_str(value["signature"], "kem_credential.signature", max_len=SIGNATURE_HEX_LEN)
    try:
        _decode_hex(value["signature"], SIGNATURE_HEX_LEN, "kem_credential.signature")
    except _IdkitMalformed as exc:
        raise SchemaError(str(exc)) from None
    try:
        raw = canonical_json(value)
    except _IdkitMalformed as exc:
        raise SchemaError(f"kem_credential is not canonical-JSON-safe: {exc}") from None
    if len(raw) > MAX_KEM_CREDENTIAL_BYTES:
        raise SchemaError(f"kem_credential exceeds {MAX_KEM_CREDENTIAL_BYTES} canonical bytes")
    if value["persona"] != persona_pub:
        # A claim cannot bind another persona's credential onto its record.
        raise SchemaError("kem_credential.persona must equal member.claim.persona_pub")


def _profile_size_ok(profile: object) -> None:
    if not isinstance(profile, dict):
        raise SchemaError("profile must be an object")
    try:
        raw = canonical_json(profile)
    except _IdkitMalformed as exc:
        raise SchemaError(f"profile is not canonical-JSON-safe: {exc}") from None
    if len(raw) > 2048:
        raise SchemaError("profile exceeds 2048 canonical bytes")


# -- per-type payload validators ---------------------------------------------


def _v_genesis(p: dict) -> None:
    _require_fields(p, "genesis", frozenset({"org", "root_pub"}))
    _require_str(p["org"], "genesis.org", max_len=128)
    _require_key(p["root_pub"], "genesis.root_pub")


def _v_delegate(p: dict) -> None:
    _require_fields(
        p, "delegate", frozenset({"child_pub", "scope", "can_redelegate"}), frozenset({"ttl"})
    )
    _require_key(p["child_pub"], "delegate.child_pub")
    validate_scope_list(p["scope"], "delegate.scope")
    if not isinstance(p["can_redelegate"], bool):
        raise SchemaError("delegate.can_redelegate must be a boolean")
    if "ttl" in p:
        if type(p["ttl"]) is not int or p["ttl"] <= 0 or p["ttl"] > 2**63 - 1:
            raise SchemaError("delegate.ttl must be a positive integer (milliseconds)")


def _v_revoke(p: dict) -> None:
    _require_fields(p, "revoke", frozenset(), frozenset({"target_event", "target_key", "reason"}))
    has_event = "target_event" in p
    has_key = "target_key" in p
    if has_event == has_key:
        raise SchemaError("revoke must carry exactly one of target_event / target_key")
    if has_event:
        _require_hash(p["target_event"], "revoke.target_event")
    else:
        _require_key(p["target_key"], "revoke.target_key")
    if "reason" in p:
        _require_str(p["reason"], "revoke.reason")


def _v_role_define(p: dict) -> None:
    _require_fields(p, "role.define", frozenset({"name", "scope_set", "claim_requires", "version"}))
    _require_role_name(p["name"], "role.define.name")
    validate_scope_list(p["scope_set"], "role.define.scope_set", allow_empty=True)
    if p["claim_requires"] not in CLAIM_REQUIRES:
        raise SchemaError(f"role.define.claim_requires must be one of {list(CLAIM_REQUIRES)}")
    if type(p["version"]) is not int or p["version"] < 1 or p["version"] > 2**31:
        raise SchemaError("role.define.version must be a positive integer")


def _v_role_grant(p: dict) -> None:
    _require_fields(p, "role.grant", frozenset({"persona", "role"}))
    _require_key(p["persona"], "role.grant.persona")
    _require_role_name(p["role"], "role.grant.role")


def _v_role_revoke(p: dict) -> None:
    _require_fields(p, "role.revoke", frozenset({"persona", "role"}), frozenset({"reason"}))
    _require_key(p["persona"], "role.revoke.persona")
    _require_role_name(p["role"], "role.revoke.role")
    if "reason" in p:
        _require_str(p["reason"], "role.revoke.reason")


def _v_invite(p: dict) -> None:
    _require_fields(
        p,
        "invite",
        frozenset({"granted_role", "expiry", "sponsor"}),
        frozenset({"invite_pub", "token_hash"}),
    )
    has_pub = "invite_pub" in p
    has_token = "token_hash" in p
    if has_pub == has_token:
        raise SchemaError("invite must carry exactly one of invite_pub / token_hash")
    if has_pub:
        _require_key(p["invite_pub"], "invite.invite_pub")
    else:
        _require_hash(p["token_hash"], "invite.token_hash")
    _require_role_name(p["granted_role"], "invite.granted_role")
    _require_ts(p["expiry"], "invite.expiry")
    _require_key(p["sponsor"], "invite.sponsor")


def _v_member_claim(p: dict) -> None:
    _require_fields(
        p,
        "member.claim",
        frozenset({"invite_ref", "persona_pub", "profile", "approvals"}),
        frozenset({"token", "kem_credential"}),
    )
    _require_hash(p["invite_ref"], "member.claim.invite_ref")
    _require_key(p["persona_pub"], "member.claim.persona_pub")
    _profile_size_ok(p["profile"])
    _require_approvals(p["approvals"], "member.claim.approvals")
    if "token" in p:
        _require_str(p["token"], "member.claim.token", max_len=128)
    if "kem_credential" in p:
        _require_kem_credential(p["kem_credential"], p["persona_pub"])


def _v_member_rekey(p: dict) -> None:
    _require_fields(p, "member.rekey", frozenset({"persona", "old_pub", "new_pub", "approvals"}))
    _require_key(p["persona"], "member.rekey.persona")
    _require_key(p["old_pub"], "member.rekey.old_pub")
    _require_key(p["new_pub"], "member.rekey.new_pub")
    _require_approvals(p["approvals"], "member.rekey.approvals")


def _v_key_rotate(p: dict) -> None:
    _require_fields(p, "key.rotate", frozenset({"old_pub", "new_pub", "continuity"}))
    _require_key(p["old_pub"], "key.rotate.old_pub")
    _require_key(p["new_pub"], "key.rotate.new_pub")
    _require_str(p["continuity"], "key.rotate.continuity", max_len=SIGNATURE_HEX_LEN)
    try:
        _decode_hex(p["continuity"], SIGNATURE_HEX_LEN, "key.rotate.continuity")
    except _IdkitMalformed as exc:
        raise SchemaError(str(exc)) from None


def _v_checkpoint(p: dict) -> None:
    _require_fields(p, "checkpoint", frozenset({"state_hash", "signers"}))
    _require_hash(p["state_hash"], "checkpoint.state_hash")
    signers = p["signers"]
    if not isinstance(signers, list) or not signers or len(signers) > MAX_APPROVALS:
        raise SchemaError(f"checkpoint.signers must be a non-empty list of at most {MAX_APPROVALS} keys")
    for s in signers:
        _require_key(s, "checkpoint.signers entry")
    if signers != sorted(set(signers)):
        raise SchemaError("checkpoint.signers must be sorted and free of duplicates")


#: The closed authority vocabulary (L8). Nothing else enters the ledger.
EVENT_TYPES = {
    "genesis": _v_genesis,
    "delegate": _v_delegate,
    "revoke": _v_revoke,
    "role.define": _v_role_define,
    "role.grant": _v_role_grant,
    "role.revoke": _v_role_revoke,
    "invite": _v_invite,
    "member.claim": _v_member_claim,
    "member.rekey": _v_member_rekey,
    "key.rotate": _v_key_rotate,
    "checkpoint": _v_checkpoint,
}


def validate_payload(payload: object) -> str:
    """Schema-check *payload*; return its type. SchemaError enforces L8."""
    if not isinstance(payload, dict):
        raise SchemaError("payload must be a JSON object")
    kind = payload.get("type")
    if not isinstance(kind, str):
        raise SchemaError("payload.type must be a string")
    validator = EVENT_TYPES.get(kind)
    if validator is None:
        raise SchemaError(
            f"event type {kind!r} is outside the authority vocabulary "
            "(the ledger holds authority events only — L8)"
        )
    validator(payload)
    return kind


# -- the event ----------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    author_key: str
    parents: tuple
    hlc: HLC
    payload: dict
    sig: str
    v: int = EVENT_VERSION
    event_id: str = field(init=False, compare=False, default="")

    def __post_init__(self):
        object.__setattr__(
            self, "event_id", hashlib.sha256(canonical_json(self.to_dict())).hexdigest()
        )

    @property
    def type(self) -> str:
        return self.payload["type"]

    # -- serialization --------------------------------------------------------

    def payload_dict(self) -> dict:
        """The signed portion: everything except ``sig``."""
        return {
            "v": self.v,
            "author_key": self.author_key,
            "parents": list(self.parents),
            "hlc": self.hlc.to_list(),
            "payload": self.payload,
        }

    def signing_input(self) -> bytes:
        return EVENT_DOMAIN + canonical_json(self.payload_dict())

    def to_dict(self) -> dict:
        data = self.payload_dict()
        data["sig"] = self.sig
        return data

    def to_json(self) -> bytes:
        """Canonical wire bytes (full event including signature)."""
        return canonical_json(self.to_dict())

    def verify_sig(self) -> None:
        """Raise :class:`SignatureError` unless the author signature holds."""
        try:
            verify_signature(self.author_key, self.sig, self.signing_input())
        except _IdkitSignatureError as exc:
            raise SignatureError(f"event {self.event_id[:12]} signature does not verify") from exc
        except _IdkitMalformed as exc:
            raise MalformedEventError(str(exc)) from exc

    # -- parsing --------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: object) -> "Event":
        if not isinstance(data, dict):
            raise MalformedEventError("event must be a JSON object")
        unknown = set(data) - _EVENT_FIELDS
        if unknown:
            raise MalformedEventError(f"event carries unknown fields: {sorted(unknown)}")
        missing = _EVENT_FIELDS - set(data)
        if missing:
            raise MalformedEventError(f"event is missing fields: {sorted(missing)}")
        if data["v"] != EVENT_VERSION:
            raise MalformedEventError(f"unsupported event version: {data['v']!r}")

        author = data["author_key"]
        try:
            _decode_hex(author, PUBLIC_KEY_HEX_LEN, "author_key")
            _decode_hex(data["sig"], SIGNATURE_HEX_LEN, "sig")
        except _IdkitMalformed as exc:
            raise MalformedEventError(str(exc)) from None

        parents = data["parents"]
        if not isinstance(parents, list) or len(parents) > MAX_PARENTS:
            raise MalformedEventError(f"parents must be a list of at most {MAX_PARENTS} hashes")
        for parent in parents:
            try:
                _decode_hex(parent, EVENT_HASH_HEX_LEN, "parent hash")
            except _IdkitMalformed as exc:
                raise MalformedEventError(str(exc)) from None
        if parents != sorted(set(parents)):
            raise MalformedEventError("parents must be sorted and free of duplicates")

        hlc = HLC.from_value(data["hlc"])
        kind = validate_payload(data["payload"])
        if kind == "genesis":
            if parents:
                raise MalformedEventError("genesis must have no parents")
        elif not parents:
            raise MalformedEventError(f"{kind} event must have at least one parent")

        return cls(
            author_key=author,
            parents=tuple(parents),
            hlc=hlc,
            payload=data["payload"],
            sig=data["sig"],
        )

    @classmethod
    def from_json(cls, raw) -> "Event":
        """Parse canonical wire bytes. Strictly anti-malleable: any byte form
        other than the one :meth:`to_json` produces — reordered keys,
        whitespace, unicode escapes, duplicate keys — is rejected, so a given
        event has exactly one accepted wire encoding (and thus one id)."""
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MalformedEventError("event bytes are not valid UTF-8") from exc
        if not isinstance(raw, str):
            raise MalformedEventError("event JSON must be str or bytes")
        if len(raw) > MAX_EVENT_BYTES:
            raise MalformedEventError(f"event exceeds {MAX_EVENT_BYTES} bytes")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise MalformedEventError("event is not valid JSON") from exc
        event = cls.from_dict(data)
        if event.to_json() != raw.encode("utf-8"):
            raise MalformedEventError("event is not in canonical wire form")
        return event


def make_event(author: KeyPair, payload: dict, parents, hlc: HLC) -> Event:
    """Mint and sign an event. Validates the payload schema (L8) first."""
    validate_payload(payload)
    parents = sorted(set(parents))
    unsigned = {
        "v": EVENT_VERSION,
        "author_key": author.public_hex,
        "parents": parents,
        "hlc": hlc.to_list(),
        "payload": payload,
    }
    sig = author.sign_hex(EVENT_DOMAIN + canonical_json(unsigned))
    return Event(
        author_key=author.public_hex,
        parents=tuple(parents),
        hlc=hlc,
        payload=payload,
        sig=sig,
    )


# -- approvals & continuity ----------------------------------------------------


def approval_core(kind: str, payload: dict) -> dict:
    """The dict an approval signature covers, per approvable event kind."""
    if kind == "member.claim":
        return {
            "kind": "member.claim",
            "invite_ref": payload["invite_ref"],
            "persona_pub": payload["persona_pub"],
        }
    if kind == "member.rekey":
        return {
            "kind": "member.rekey",
            "persona": payload["persona"],
            "old_pub": payload["old_pub"],
            "new_pub": payload["new_pub"],
        }
    raise MalformedEventError(f"event kind {kind!r} does not take approvals")


def approval_signing_input(kind: str, payload: dict) -> bytes:
    return APPROVAL_DOMAIN + canonical_json(approval_core(kind, payload))


def sign_approval(approver: KeyPair, kind: str, payload: dict) -> dict:
    """An ``{key, sig}`` approval entry for a claim/rekey payload."""
    return {
        "key": approver.public_hex,
        "sig": approver.sign_hex(approval_signing_input(kind, payload)),
    }


def rotate_continuity_input(old_pub: str, new_pub: str) -> bytes:
    return ROTATE_DOMAIN + canonical_json({"old_pub": old_pub, "new_pub": new_pub})


def sign_rotate_continuity(new_key: KeyPair, old_pub: str) -> str:
    """The continuity proof: the NEW root key signs the rotation binding,
    proving possession — a rotation cannot point at a key its author does
    not control."""
    return new_key.sign_hex(rotate_continuity_input(old_pub, new_key.public_hex))
