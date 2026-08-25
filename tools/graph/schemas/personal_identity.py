"""Personal identity Settings — the person's root key + passkey credentials.

Two contracts back the identity/auth onboarding flow (model notes
``graph://53f65f2f-d73`` two-gate matrix, ``graph://80ef5131-9f0``
password-as-access-floor):

* ``autonomy.identity.personal#1`` — the PERSONAL root: one Ed25519
  keypair per person, DISTINCT from the org identity
  (``autonomy.network.org-key``). The personal identity is your root —
  org membership is a delegation FROM it, never the other way around.
  Stored exactly like the org key: only the armored, password-encrypted
  blob (invariant I1 — plaintext exists solely in the operator's browser
  during ceremonies), gated at this schema layer so every write path
  (settings_ops, dashboard routes, POST /api/graph/setting) shares one
  fail-closed check.
* ``autonomy.identity.passkey#1`` — WebAuthn/FIDO2 credentials enrolled
  per device/install (Gate 1: dashboard ACCESS, never signing). Rows
  carry what assertion verification needs: credential id, COSE public
  key, sign count, and the RP ID the credential is domain-bound to
  (passkeys minted on ``localhost`` and on the ``.ts.net`` name are
  DIFFERENT credentials — the rp_id column is why).

The armor discipline is shared with :mod:`.network_identity`: the
``cryptography``-backed verifier is imported lazily at validation time
and the write is refused when it is unavailable (I1 fail-closed).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
    singleton,
)


PERSONAL_IDENTITY_SET_ID = "autonomy.identity.personal"
PERSONAL_IDENTITY_REVISION = 1
PASSKEY_SET_ID = "autonomy.identity.passkey"
PASSKEY_REVISION = 1
FACTOR_METADATA_SET_ID = "autonomy.identity.factor-metadata"
FACTOR_METADATA_REVISION = 1
FACTOR_RECIPIENT_METADATA_SET_ID = "autonomy.identity.factor-recipient-metadata"
FACTOR_RECIPIENT_METADATA_REVISION = 1

#: Raw Ed25519 public key, lowercase hex (= the key id) — pinned to A1.
PERSONAL_PUB_HEX_LEN = 64

#: WebAuthn credential IDs are at most 1023 bytes (spec §5.8.3); base64url
#: of that is < 1400 chars. COSE keys for the algorithms we accept
#: (EdDSA/ES256/RS256) are far below 2KB even base64url-encoded.
MAX_CREDENTIAL_ID_B64 = 1400
MAX_PUBLIC_KEY_B64 = 2800

#: Registrable-domain shape for a WebAuthn RP ID: DNS labels, no scheme,
#: no port, no trailing dot. ``localhost`` is a valid single label.
_RP_ID_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)*$"
)

#: base64url without padding — the wire form WebAuthn hands the browser.
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Known AuthenticatorTransport values (WebAuthn L3); stored as hints
#: for re-authentication UX, never trusted for anything security-bearing.
PASSKEY_TRANSPORTS = (
    "ble", "cable", "hybrid", "internal", "nfc", "smart-card", "usb",
)


SYNOPSIS = {
    "summary": (
        "Personal identity state: the person's root key as an armored, "
        "password-encrypted blob (autonomy.identity.personal — encrypted "
        "armor only, plaintext never leaves the operator's browser; "
        "distinct from the ORG key at autonomy.network.org-key), and the "
        "WebAuthn passkey credentials enrolled per device "
        "(autonomy.identity.passkey — credential id, COSE public key, "
        "sign count, RP ID; Gate-1 dashboard access, never signing)."
    ),
    "nouns": [
        "personal identity", "personal root key", "passkey", "WebAuthn",
        "FIDO2", "credential", "device enrollment", "onboarding",
        "get started", "Face ID", "Touch ID", "dashboard access",
        "encrypted private key",
    ],
    "related_set_ids": [
        "autonomy.network.org-key#1",
        "autonomy.network.binding#1",
        "autonomy.commit.signing-key#1",
    ],
}


def _require_str(payload: dict, key: str, cls_name: str, *, max_len: int = 512) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must be a non-empty string of at most {max_len} chars"
        )
    return value


def _require_iso_ts(payload: dict, key: str, cls_name: str) -> str:
    value = _require_str(payload, key, cls_name, max_len=32)
    try:
        datetime.strptime(value, _ISO_FORMAT)
    except ValueError as exc:
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must be an ISO-8601 UTC timestamp "
            f"like 2026-07-17T00:00:00Z, got {value!r}"
        ) from exc
    return value


# ── autonomy.identity.personal ────────────────────────────────


#: The operator's own store. Observed: every stored row lives there
#: and none in any organization's database. Declared so it is
#: enforced rather than agreed -- an undeclared home refuses
#: nothing, and a plain read looks in the caller's own store and
#: reports nothing for rows sitting one database over.
#: Banded because the row CARRIES key material, not because anything
#: labelled it secret. Every write passing state="raw" by hand is a
#: convention; a band is enforced at write, at promote, and at the
#: federated read, each of which fails independently.
@home("personal")
@publication_band(max="raw")
@singleton(key="default")
class PersonalIdentityV1(SettingSchema):
    """The person's root key — encrypted armor only (I1).

    Key: a label (``default`` for the one identity a single-operator
    dashboard holds). Payload: the armored, password-encrypted Ed25519
    root private key plus the person's display name. The password (not
    the passkey) decrypts this — access and signing stay separate
    (two-gate model, 53f65f2f-d73).
    """

    set_id = PERSONAL_IDENTITY_SET_ID
    schema_revision = PERSONAL_IDENTITY_REVISION

    armored_private_key: str = field(
        required=True,
        description=(
            "The armored, factor-protected Ed25519 personal root private key "
            "in the CANONICAL idkit byte form (tools/network/idkit/armor.py "
            "canonicalize_armor). Encrypted at rest; only ever decrypted in "
            "the operator's browser with factors whose secret material never "
            "reaches the server (I1)."
        ),
    )
    root_pub: str = field(
        required=False,
        description=(
            "Hex-encoded public half (64 lowercase hex chars = the key id). "
            "Public by definition; the WebAuthn user handle and future "
            "delegation certs anchor on it."
        ),
    )
    display_name: str = field(
        required=True,
        description="The person's chosen display name ('Your name' in onboarding).",
    )
    created_at: str = field(
        required=True,
        description="ISO-8601 UTC timestamp the identity was created.",
    )
    require_pair: bool = field(
        required=False,
        description=(
            "The MFA policy: when true the root armor must keep BOTH a password "
            "and a passkey factor (a required pair). Set by the sign-on UI's "
            "re-arm flow; absent means false (single-factor allowed)."
        ),
    )
    updated_at: str = field(
        required=False,
        description="ISO-8601 UTC timestamp of the last re-arm, if any.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        if "require_pair" in payload and not isinstance(payload["require_pair"], bool):
            raise SchemaValidationError(
                f"{cls.__name__}: 'require_pair' must be a boolean"
            )
        armor = _require_str(
            # The v3 bound permits 32 logical factors with bounded per-device
            # passkey recipient sets.  HPKE wraps and base64 expansion can
            # legitimately exceed the old 128 KiB ceiling at that declared
            # maximum, so keep the schema bound aligned with the crypto bound.
            payload, "armored_private_key", cls.__name__, max_len=1_048_576,
        )
        # I1 tripwire: a raw Ed25519 private key is exactly 64 hex chars.
        # Kept for the clearer message; the canonical check below refuses
        # it too.
        if len(armor) == 64 and _HEX_RE.match(armor):
            raise SchemaValidationError(
                f"{cls.__name__}: 'armored_private_key' looks like a raw hex "
                "Ed25519 private key — plaintext key material must never be "
                "stored (I1); store the password-encrypted armor instead"
            )

        # THE I1 gate, at the layer every write path shares (same
        # discipline as NetworkOrgKeyV1): strict parse + byte-for-byte
        # canonical equality, lazily imported, FAIL-CLOSED when the
        # verifier is unavailable.
        try:
            from tools.network.idkit.armor import (
                ArmorError,
                armor_root_pub,
                canonicalize_armor,
            )
        except Exception as exc:  # pragma: no cover — env without idkit deps
            raise SchemaValidationError(
                f"{cls.__name__}: cannot verify 'armored_private_key' — "
                f"tools.network.idkit is unavailable ({exc}); refusing the "
                "write (I1 fail-closed)"
            ) from exc
        try:
            # Version-agnostic: personal identities may retain the established
            # v2 armor or use a root-signed v3 factor-policy generation. Both
            # are strictly parsed and must already be in canonical byte form.
            armor_data = {"root_pub": armor_root_pub(armor)}
            if canonicalize_armor(armor) != armor:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'armored_private_key' must be the "
                    "canonical armor byte form — re-emit it with "
                    "tools.network.idkit.armor.canonicalize_armor (I1)"
                )
        except ArmorError as e:
            raise SchemaValidationError(
                f"{cls.__name__}: 'armored_private_key' is not a canonical "
                f"factor-protected key armor (I1 — plaintext key material "
                f"must never be stored): {e}"
            ) from e

        if "root_pub" in payload:
            root_pub = _require_str(payload, "root_pub", cls.__name__,
                                    max_len=PERSONAL_PUB_HEX_LEN)
            if len(root_pub) != PERSONAL_PUB_HEX_LEN or not _HEX_RE.match(root_pub):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'root_pub' must be exactly "
                    f"{PERSONAL_PUB_HEX_LEN} lowercase hex chars"
                )
            if root_pub != armor_data["root_pub"]:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'root_pub' does not match the armor's "
                    "enclosed public key"
                )

        _require_str(payload, "display_name", cls.__name__, max_len=120)
        _require_iso_ts(payload, "created_at", cls.__name__)


# ── autonomy.identity.factor-metadata ─────────────────────────


@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="factor_id")
class FactorMetadataV1(SettingSchema):
    """Operator-facing names for cryptographic factors.

    Names and purpose labels do not participate in key derivation or the
    root-signed armor.  Keeping them in a separate row makes rename an
    unattended metadata edit while every authority transition remains an
    atomic, signed policy-generation change.
    """

    set_id = FACTOR_METADATA_SET_ID
    schema_revision = FACTOR_METADATA_REVISION

    # The factor_id is the ROW KEY (key_strategy="factor_id") and comes back with
    # the row — repeating it in the payload invites drift, so it is not a field.
    label: str = field(
        required=True,
        description="Operator-facing factor name.",
    )
    purpose: str = field(
        required=False,
        description="Optional operator-facing explanation of this factor's purpose.",
    )
    updated_at: str = field(
        required=True,
        description="ISO-8601 UTC timestamp of the latest metadata edit.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _require_str(payload, "label", cls.__name__, max_len=120)
        if "purpose" in payload:
            _require_str(payload, "purpose", cls.__name__, max_len=240)
        _require_iso_ts(payload, "updated_at", cls.__name__)


@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="factor_recipient_id")
class FactorRecipientMetadataV1(SettingSchema):
    """Mutable operator-facing names for passkey device recipients.

    The cryptographic recipient remains frozen in the root-signed armor; its
    display name is overlaid from this row so a device rename is unattended
    metadata and never recompiles a root-policy generation.
    """

    set_id = FACTOR_RECIPIENT_METADATA_SET_ID
    schema_revision = FACTOR_RECIPIENT_METADATA_REVISION

    factor_id: str = field(required=True, description="Owning logical factor id.")
    recipient_public_key: str = field(
        required=True,
        description="The exact X25519 recipient public key being named.",
    )
    label: str = field(required=True, description="Operator-facing device name.")
    updated_at: str = field(required=True, description="Latest metadata edit time.")

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        factor_id = _require_str(payload, "factor_id", cls.__name__, max_len=128)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", factor_id):
            raise SchemaValidationError(
                f"{cls.__name__}: 'factor_id' has an invalid shape"
            )
        public_key = _require_str(
            payload, "recipient_public_key", cls.__name__, max_len=64,
        )
        if len(public_key) != 64 or not _HEX_RE.fullmatch(public_key):
            raise SchemaValidationError(
                f"{cls.__name__}: 'recipient_public_key' must be 64 lowercase hex chars"
            )
        _require_str(payload, "label", cls.__name__, max_len=120)
        _require_iso_ts(payload, "updated_at", cls.__name__)


# ── autonomy.identity.passkey ─────────────────────────────────


#: The operator's own store. Observed: every stored row lives there
#: and none in any organization's database. Declared so it is
#: enforced rather than agreed -- an undeclared home refuses
#: nothing, and a plain read looks in the caller's own store and
#: reports nothing for rows sitting one database over.
#: Pinned, though the row holds no secret. Two reasons, and the second is the
#: one that was missed: published rows expose the operator's whole DEVICE
#: INVENTORY to any peer, and — because peer composition is derived from exactly
#: this declaration — an absent band also means ``read_set`` opens peer
#: databases for a set that authentication decisions read.
@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="credential_id")
class PasskeyCredentialV1(SettingSchema):
    """One enrolled WebAuthn credential — Gate-1 access, never signing.

    Key: the credential ID (base64url, as the authenticator minted it) —
    one row per enrolled device/install. The stored COSE public key +
    sign count are what assertion verification consumes; ``rp_id``
    records the domain the credential is bound to, because a passkey
    enrolled on ``localhost`` cannot assert on the ``.ts.net`` name and
    the serving gate must know which rows can possibly match.
    """

    set_id = PASSKEY_SET_ID
    schema_revision = PASSKEY_REVISION

    credential_id: str = field(
        required=True,
        description=(
            "The WebAuthn credential ID, base64url without padding — must "
            "equal the row key. Chosen by the authenticator; an identifier, "
            "never a secret."
        ),
    )
    public_key: str = field(
        required=True,
        description=(
            "The credential's COSE-encoded public key, base64url without "
            "padding — what verify_authentication_response consumes."
        ),
    )
    sign_count: int = field(
        required=True,
        description=(
            "The authenticator's signature counter at enrollment (0 for "
            "most platform authenticators). Clone detection: assertions "
            "must never present a lower value."
        ),
    )
    rp_id: str = field(
        required=True,
        description=(
            "The Relying Party ID the credential is bound to — the request "
            "host at enrollment time (e.g. 'localhost' or the .ts.net "
            "name). Passkeys are domain-bound; this row only matches "
            "assertions on this RP ID."
        ),
    )
    origin: str = field(
        required=True,
        description=(
            "The full web origin enrollment happened on (scheme://host[:port]) "
            "— pinned at options time, recorded for audit."
        ),
    )
    label: str = field(
        required=False,
        description="Operator-facing device label (e.g. 'This device').",
    )
    transports: list = field(
        required=False,
        default_factory=list,
        element=str,
        description=(
            "AuthenticatorTransport hints returned at enrollment "
            "(e.g. ['internal', 'hybrid']); UX hints only."
        ),
    )
    aaguid: str = field(
        required=False,
        description="Authenticator AAGUID (UUID string) when the attestation exposed one.",
    )
    backup_eligible: bool = field(
        required=False,
        description=(
            "The WebAuthn BE flag as verified at enrollment: true means the "
            "credential is multi-device/sync-capable. UX metadata only."
        ),
    )
    backed_up: bool = field(
        required=False,
        description=(
            "The WebAuthn BS flag as verified at enrollment: true means the "
            "credential was backed up at that ceremony. UX metadata only."
        ),
    )
    created_at: str = field(
        required=True,
        description="ISO-8601 UTC timestamp the credential was enrolled.",
    )
    statement: dict = field(
        required=True,
        description=(
            "The root-signed enrollment statement (idkit.enrollment) — what "
            "turns this agent-writable row into evidence. Re-verified at every "
            "use against the root from autonomy.identity.personal; a passkey "
            "without one is not a legitimate recipient and cannot be promoted."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        stmt = payload.get("statement")
        if not isinstance(stmt, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: 'statement' must be the root-signed enrollment "
                "statement object"
            )
        from tools.network.idkit.enrollment import PasskeyEnrollmentStatement
        from tools.network.idkit.errors import IdkitError
        try:
            PasskeyEnrollmentStatement.from_dict(stmt)
        except IdkitError as exc:
            raise SchemaValidationError(
                f"{cls.__name__}: 'statement' is malformed: {exc}"
            ) from exc
        cred = _require_str(payload, "credential_id", cls.__name__,
                            max_len=MAX_CREDENTIAL_ID_B64)
        if not _B64URL_RE.match(cred):
            raise SchemaValidationError(
                f"{cls.__name__}: 'credential_id' must be base64url without "
                "padding (the wire form the authenticator returned)"
            )
        pub = _require_str(payload, "public_key", cls.__name__,
                           max_len=MAX_PUBLIC_KEY_B64)
        if not _B64URL_RE.match(pub):
            raise SchemaValidationError(
                f"{cls.__name__}: 'public_key' must be base64url without padding"
            )
        count = payload.get("sign_count")
        if type(count) is not int or count < 0 or count > 2**32 - 1:
            raise SchemaValidationError(
                f"{cls.__name__}: 'sign_count' must be an integer in "
                "[0, 2^32) (the authenticator's 32-bit counter)"
            )
        rp_id = _require_str(payload, "rp_id", cls.__name__, max_len=253)
        if not _RP_ID_RE.match(rp_id):
            raise SchemaValidationError(
                f"{cls.__name__}: 'rp_id' must be a lowercase registrable "
                f"domain name (no scheme, port, or trailing dot), got {rp_id!r}"
            )
        origin = _require_str(payload, "origin", cls.__name__)
        if not origin.startswith(("https://", "http://")):
            raise SchemaValidationError(
                f"{cls.__name__}: 'origin' must be a web origin "
                f"(scheme://host[:port]), got {origin!r}"
            )
        if "label" in payload:
            _require_str(payload, "label", cls.__name__, max_len=120)
        for name in ("backup_eligible", "backed_up"):
            if name in payload and not isinstance(payload[name], bool):
                raise SchemaValidationError(
                    f"{cls.__name__}: '{name}' must be a boolean"
                )
        transports = payload.get("transports")
        if transports is not None:
            if not isinstance(transports, list):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'transports' must be a list"
                )
            for i, t in enumerate(transports):
                if t not in PASSKEY_TRANSPORTS:
                    raise SchemaValidationError(
                        f"{cls.__name__}: transports[{i}] must be one of "
                        f"{PASSKEY_TRANSPORTS}, got {t!r}"
                    )
        if "aaguid" in payload:
            aaguid = _require_str(payload, "aaguid", cls.__name__, max_len=64)
            import uuid as uuid_mod
            try:
                uuid_mod.UUID(aaguid)
            except (ValueError, AttributeError) as exc:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'aaguid' must be a UUID string, got {aaguid!r}"
                ) from exc
        _require_iso_ts(payload, "created_at", cls.__name__)
