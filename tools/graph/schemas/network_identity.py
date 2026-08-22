"""auto.network identity Settings — org key, binding, link-grant cache.

Four contracts back the dashboard side of the auto.network share-link
program (spec ``graph://a17c8657-939`` §4.6, §6.2, §6.6; invariants I1, I9):

* ``autonomy.network.org-key#1`` — the org root private key as an armored,
  passphrase-encrypted blob. Same storage discipline as
  ``autonomy.commit.signing-key``: the server only ever stores and serves
  the ENCRYPTED armor; plaintext exists solely in the operator's browser
  during ceremonies (invariant I1).
* ``autonomy.network.binding#1`` — local record of the org's registry
  binding state: org UUID, root public key, recovery policy, binding
  expiry, registry URL, renewal state, signed endpoint hints.
* ``autonomy.network.link-grant#1`` — the dashboard-side grant cache. The
  dashboard serves a shared target only against a valid row here
  (invariant I9); rows carry the token, target, meta, and the issuing
  certificate's subject for attribution (invariant I6).
* ``autonomy.network.serve-cert#2`` — one org-scoped serving child key with
  two direct-root, short-lived ``tunnel:serve`` certificates: a persona
  certificate used only for registry admission and an identity-neutral
  certificate used only in viewer handshakes. The row points to the
  on-disk (mode-0600) key file the unattended connector signs with. The
  signing key is a filesystem credential, never settings-store data, so
  I1's no-plaintext-key discipline holds.

Vocabulary is pinned to A1 (``tools/network/idkit``): subject kinds,
token shape, and key-id hex length are duplicated here as constants so
this module stays importable without ``cryptography`` installed;
``test_network_identity_schemas.py`` asserts they match idkit exactly.
The org-key validator DOES import idkit — lazily, at validation time —
because the strict/canonical armor check is the I1 gate for every write
path (settings_ops, the dashboard route, POST /api/graph/setting) and
must fail closed when the verifier is unavailable.

Rung-2 reservations: subject kind ``persona`` and grant meta
``require_auth: true`` appear in the schema now but are rejected by
validators with a clear message until Track E (session linking) lands.
"""

from __future__ import annotations

import re
import uuid as uuid_mod
from datetime import datetime
from typing import Any

from .registry import (
    publication_band,
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    singleton,
)


NETWORK_ORG_KEY_SET_ID = "autonomy.network.org-key"
NETWORK_ORG_KEY_REVISION = 1
NETWORK_ORG_KEY_REVISION_2 = 2
#: The sealing purpose label binding an org-root seal to the owner's
#: personal-root-derived X25519 recipient key (register B4, Option B).
ORG_ROOT_ARMOR_PURPOSE = "autonomy/org-root-armor/v1"
#: Hex length of a 32-byte seed sealed under idkit sealing suite 1:
#: 1 suite byte + 32 enc + 32 ct + 16 tag.
_SEALED_ROOT_KEY_HEX_LEN = 2 * (1 + 32 + 32 + 16)
NETWORK_BINDING_SET_ID = "autonomy.network.binding"
NETWORK_BINDING_REVISION = 1
#: Which persona this node holds the seed for, per org. Written org=None
#: (personal.db), keyed by the org's genesis id. See NetworkPersonaV1 for why
#: the scope is the load-bearing part.
NETWORK_PERSONA_SET_ID = "autonomy.network.persona"
NETWORK_PERSONA_REVISION = 1
#: Hex length of an org genesis event id. Mirrors idkit's persona derivation,
#: which anchors on the genesis id string.
GENESIS_ID_HEX_LEN = 64
#: The ceremony that produced a persona. There is no third: a persona comes
#: into existence either by founding an org or by claiming membership.
PERSONA_SOURCES = ("found", "join")
NETWORK_LINK_GRANT_SET_ID = "autonomy.network.link-grant"
NETWORK_LINK_GRANT_REVISION = 3
NETWORK_PUBLIC_LINK_BASE_URL = "https://relay.auto.network"
NETWORK_SERVE_CERT_SET_ID = "autonomy.network.serve-cert"
NETWORK_SERVE_CERT_REVISION = 2
SERVE_CERT_SCOPE = "tunnel:serve"


# ── A1 vocabulary pins (tools/network/idkit) ─────────────────
#
# Duplicated (not imported) so tools.graph never pulls the `cryptography`
# dependency at import time; a test asserts these match idkit's exports.

SUBJECT_KINDS_NETWORK = ("agent", "machine", "operator", "persona")
RESERVED_SUBJECT_KINDS = ("persona",)  # rung 2: session linking (Track E)
NETWORK_TOKEN_HEX_LEN = 32  # 128-bit CSPRNG token, lowercase hex (I2)
NETWORK_PUB_HEX_LEN = 64  # raw Ed25519 public key, lowercase hex (= key id)

TARGET_TYPES = (
    "design",
    "file",
    "mission",
    "note",
    "present",
    "org:join",
    "fleet:join",
    "fleet:sync",
)  # §6.1 artifact + membership resolvers

RECOVERY_MODES = ("none", "recovery-key", "org-vouch", "blindhash-escrow")
RESERVED_RECOVERY_MODES = ("org-vouch", "blindhash-escrow")  # companion ledger spec

GRANT_META_KEYS = frozenset(
    {"ttl", "label", "require_auth", "participant_id", "ice_policy"}
)
ICE_POLICIES = ("direct_allowed", "relay_only")

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_HEX_RE = re.compile(r"^[0-9a-f]+$")


SYNOPSIS = {
    "summary": (
        "auto.network identity state: the org root key as an armored, "
        "passphrase-encrypted blob (autonomy.network.org-key — encrypted "
        "armor only, plaintext never leaves the operator's browser), the "
        "org's registry binding record (autonomy.network.binding — UUID, "
        "root pub, recovery policy, TTL/renewal, endpoint hints), and the "
        "dashboard-side share-link grant cache (autonomy.network.link-grant "
        "— token, target, meta, issuing cert subject; the I9 serving check "
        "reads this), and one serving child certified separately for "
        "registry admission and identity-neutral viewer authentication."
    ),
    "nouns": [
        "network identity", "org root key", "auto.network", "share link",
        "link grant", "grant cache", "registry binding", "org binding",
        "recovery policy", "endpoint hints", "delegation", "token",
        "encrypted private key",
    ],
    "related_set_ids": [
        "autonomy.commit.signing-key#1",
        "autonomy.capability.operation_policy#1",
    ],
}


# ── shared validators ─────────────────────────────────────────


def _require_str(payload: dict, key: str, cls_name: str, *, max_len: int = 512) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must be a non-empty string of at most {max_len} chars"
        )
    return value


def _require_hex(payload: dict, key: str, cls_name: str, *, length: int) -> str:
    value = _require_str(payload, key, cls_name, max_len=length)
    if len(value) != length or not _HEX_RE.match(value):
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must be exactly {length} lowercase hex chars"
        )
    return value


def _require_uuid(payload: dict, key: str, cls_name: str) -> str:
    value = _require_str(payload, key, cls_name, max_len=64)
    try:
        uuid_mod.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must be a valid UUID, got {value!r}"
        ) from exc
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


# ── autonomy.network.org-key ──────────────────────────────────


#: An organization's own root key lives in that organization's database
#: -- there is one per org and it means nothing outside it.
#:
#: Banded because the row carries the SEALED ROOT SEED. Every write
#: passing state="raw" by hand is a convention; a band is enforced at
#: write, at promote, and at the federated read, each of which fails
#: independently.
@home("organization")
@publication_band(max="raw")
@singleton(key="default")
class NetworkOrgKeyV2(SettingSchema):
    """Revision 2 — the org root seed SEALED to the owner's key (B4/Option B).

    The seed is hybrid-public-key-encrypted (idkit sealing, auto-x9etu)
    to an X25519 recipient key derived from the owner's PERSONAL root
    under :data:`ORG_ROOT_ARMOR_PURPOSE` — one personal password unlocks
    every owned org, and re-sealing to another party's recipient key
    transfers or shares ownership without exposing the seed. Parallel to
    revision 1 (legacy passphrase-PBKDF2 armor, readable until the
    retrofit re-seals); no upconvert chain exists between them by design.
    I1 holds: only the sealed record is stored, never plaintext.
    """

    set_id = NETWORK_ORG_KEY_SET_ID
    schema_revision = NETWORK_ORG_KEY_REVISION_2

    root_pub: str = field(
        required=True,
        description="Hex org root public key (64 lowercase hex chars = key id).",
    )
    sealed_root_key: str = field(
        required=True,
        description=(
            "Hex of the idkit sealing wire record over the 32-byte org root "
            "seed, sealed to owner_kem_pub under seal_purpose. Opens only "
            "with the recipient key derived from the owner's personal root."
        ),
    )
    owner_kem_pub: str = field(
        required=True,
        description=(
            "The owner's X25519 recipient public key (64 lowercase hex), "
            "derived from the personal root under seal_purpose — re-derivable "
            "from the personal seed, stored for tooling and transfer flows."
        ),
    )
    seal_purpose: str = field(
        required=True,
        description=f"Must be exactly {ORG_ROOT_ARMOR_PURPOSE!r}.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _require_hex(payload, "root_pub", cls.__name__, length=NETWORK_PUB_HEX_LEN)
        _require_hex(payload, "owner_kem_pub", cls.__name__, length=NETWORK_PUB_HEX_LEN)
        sealed = _require_str(
            payload, "sealed_root_key", cls.__name__, max_len=1024
        )
        # I1 tripwire: 64 hex chars is a raw Ed25519 seed, and nothing but
        # the fixed-size sealed record shape is storable at all.
        if len(sealed) != _SEALED_ROOT_KEY_HEX_LEN or not _HEX_RE.match(sealed):
            raise SchemaValidationError(
                f"{cls.__name__}: 'sealed_root_key' must be the "
                f"{_SEALED_ROOT_KEY_HEX_LEN}-char lowercase-hex idkit sealed "
                "record — plaintext or foreign formats are refused (I1)"
            )
        if payload.get("seal_purpose") != ORG_ROOT_ARMOR_PURPOSE:
            raise SchemaValidationError(
                f"{cls.__name__}: 'seal_purpose' must be exactly "
                f"{ORG_ROOT_ARMOR_PURPOSE!r} — the purpose label is part of "
                "the sealing context and is not caller-chosen"
            )


# ── autonomy.network.binding ──────────────────────────────────


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@publication_band(min="raw", max="raw")
@home("organization")
@keyed_per_entity(key_strategy="registry_host")
class NetworkBindingV1(SettingSchema):
    """Local record of the org's auto.network registry binding (§4.1–4.3).

    Key: the registry authority this binding lives on, canonically its
    host (e.g. ``auto.network``) — one row per registry the org is bound
    to. The registry's own copy is authoritative; this row is the
    dashboard's working state for renewal heartbeats and ceremony UIs.
    """

    set_id = NETWORK_BINDING_SET_ID
    schema_revision = NETWORK_BINDING_REVISION

    org_uuid: str = field(
        required=True,
        description=(
            "The org's UUID on the registry. A name, not an authority — "
            "the root key is the authority (first-come binding, §4.1)."
        ),
    )
    root_pub: str = field(
        required=True,
        description=(
            "Hex-encoded Ed25519 org root public key the binding is "
            "anchored to (64 lowercase hex chars = the key id)."
        ),
    )
    registry_url: str = field(
        required=True,
        description="Base URL of the registry this binding lives on.",
    )
    recovery_policy: dict = field(
        required=True,
        description=(
            "Pre-declared rebind policy (I3: no rebind path outside it). "
            "{mode: none | recovery-key | org-vouch | blindhash-escrow, "
            "recovery_pub: hex pub, required iff mode=recovery-key}. "
            "org-vouch and blindhash-escrow are RESERVED (companion "
            "ledger spec) and rejected for now."
        ),
    )
    binding_expires_at: str = field(
        required=True,
        description=(
            "ISO-8601 UTC expiry of the binding TTL (§4.2, default 30d); "
            "renewal heartbeats push it forward (I7: everything carries "
            "a TTL)."
        ),
    )
    last_renewed_at: str = field(
        required=False,
        description="ISO-8601 UTC timestamp of the last successful renewal heartbeat.",
    )
    endpoint_hints: list = field(
        required=False,
        default_factory=list,
        element={"url": str},
        description=(
            "Org-signed endpoint hints served to identified sessions "
            "(§6.8): each entry names a dashboard endpoint ({url, ...}); "
            "endpoints must still prove themselves via /.well-known/"
            "autonomy + cert chain (I11). The URL is an identifier, never "
            "a secret (I10)."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _require_uuid(payload, "org_uuid", cls.__name__)
        _require_hex(payload, "root_pub", cls.__name__, length=NETWORK_PUB_HEX_LEN)
        registry_url = _require_str(payload, "registry_url", cls.__name__)
        if not registry_url.startswith(("https://", "http://")):
            raise SchemaValidationError(
                f"{cls.__name__}: 'registry_url' must be an http(s) URL, got {registry_url!r}"
            )
        _require_iso_ts(payload, "binding_expires_at", cls.__name__)
        if "last_renewed_at" in payload:
            _require_iso_ts(payload, "last_renewed_at", cls.__name__)

        policy = payload.get("recovery_policy")
        if not isinstance(policy, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: 'recovery_policy' must be an object"
            )
        mode = policy.get("mode")
        if mode not in RECOVERY_MODES:
            raise SchemaValidationError(
                f"{cls.__name__}: recovery_policy.mode must be one of "
                f"{RECOVERY_MODES}, got {mode!r}"
            )
        if mode in RESERVED_RECOVERY_MODES:
            raise SchemaValidationError(
                f"{cls.__name__}: recovery_policy.mode {mode!r} is RESERVED "
                "for the org authority ledger (companion spec); declare "
                "'none' or 'recovery-key' for now"
            )
        if mode == "recovery-key":
            _require_hex(policy, "recovery_pub", cls.__name__, length=NETWORK_PUB_HEX_LEN)
        elif "recovery_pub" in policy:
            raise SchemaValidationError(
                f"{cls.__name__}: recovery_policy.recovery_pub is only valid "
                "with mode='recovery-key'"
            )

        hints = payload.get("endpoint_hints")
        if hints is not None:
            if not isinstance(hints, list):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'endpoint_hints' must be a list"
                )
            for i, hint in enumerate(hints):
                if not isinstance(hint, dict) or not isinstance(hint.get("url"), str) \
                        or not hint.get("url"):
                    raise SchemaValidationError(
                        f"{cls.__name__}: endpoint_hints[{i}] must be an "
                        "object with a non-empty 'url'"
                    )


# ── autonomy.network.link-grant ───────────────────────────────


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@publication_band(min="raw", max="raw")
@home("organization")
@keyed_per_entity(key_strategy="grant_token")
class NetworkLinkGrantV1(SettingSchema):
    """Dashboard-side share-link grant cache (§4.4, §6.1; feeds I9).

    Key: the grant token itself — the same 32-hex string the registry
    minted, so the serving path's I9 check is one keyed read. The token is
    pure CSPRNG output, never derived from the target (I2 — enforced by
    construction in ``tools/network/idkit``); this row is where the
    token→target mapping lives on the dashboard side.
    """

    set_id = NETWORK_LINK_GRANT_SET_ID
    schema_revision = 1

    token: str = field(
        required=True,
        description=(
            "The 128-bit grant token as 32 lowercase hex chars — CSPRNG "
            "output, never target-derived (I2). Must equal the row key."
        ),
    )
    target_uuid: str = field(
        required=True,
        description="UUID of the shared artifact this grant resolves to.",
    )
    target_type: str = field(
        required=True,
        enum=list(TARGET_TYPES),
        description="Artifact resolver kind (§6.1).",
    )
    meta: dict = field(
        required=False,
        default_factory=dict,
        description=(
            "Grant metadata: {ttl: seconds > 0, label: str, require_auth: "
            "bool, ice_policy: direct_allowed|relay_only}. ice_policy is "
            "immutable for the life of this signed grant; omission means "
            "direct_allowed. require_auth=true is RESERVED for rung 2 "
            "(Track E session linking; registry answers 501 until then) "
            "and is rejected for now."
        ),
    )
    subject: dict = field(
        required=True,
        description=(
            "The issuing delegation certificate's subject {kind, id} — "
            "every grant is attributable to a specific certificate "
            "subject (I6). kind 'persona' is RESERVED for rung 2."
        ),
    )
    issued_at: str = field(
        required=True,
        description="ISO-8601 UTC timestamp the grant was issued.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        token = _require_str(payload, "token", cls.__name__, max_len=NETWORK_TOKEN_HEX_LEN)
        if len(token) != NETWORK_TOKEN_HEX_LEN or not _HEX_RE.match(token):
            raise SchemaValidationError(
                f"{cls.__name__}: 'token' must be exactly "
                f"{NETWORK_TOKEN_HEX_LEN} lowercase hex chars (a 128-bit "
                "CSPRNG token, I2)"
            )
        _require_uuid(payload, "target_uuid", cls.__name__)
        target_type = _require_str(payload, "target_type", cls.__name__, max_len=32)
        if target_type not in TARGET_TYPES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'target_type' must be one of {TARGET_TYPES}, "
                f"got {target_type!r}"
            )
        _require_iso_ts(payload, "issued_at", cls.__name__)

        meta = payload.get("meta")
        if meta is not None:
            if not isinstance(meta, dict):
                raise SchemaValidationError(f"{cls.__name__}: 'meta' must be an object")
            unknown = set(meta) - GRANT_META_KEYS
            if unknown:
                raise SchemaValidationError(
                    f"{cls.__name__}: meta carries unknown keys {sorted(unknown)}; "
                    f"allowed: {sorted(GRANT_META_KEYS)}"
                )
            if "ttl" in meta:
                ttl = meta["ttl"]
                if type(ttl) is not int or ttl <= 0:
                    raise SchemaValidationError(
                        f"{cls.__name__}: meta.ttl must be a positive integer "
                        "of seconds"
                    )
            if "label" in meta and (not isinstance(meta["label"], str) or not meta["label"]):
                raise SchemaValidationError(
                    f"{cls.__name__}: meta.label must be a non-empty string"
                )
            if "require_auth" in meta:
                if not isinstance(meta["require_auth"], bool):
                    raise SchemaValidationError(
                        f"{cls.__name__}: meta.require_auth must be a boolean"
                    )
                if meta["require_auth"]:
                    raise SchemaValidationError(
                        f"{cls.__name__}: meta.require_auth=true is RESERVED "
                        "for rung 2 (Track E session linking); the registry "
                        "answers 501 for it today — issue the grant without "
                        "require_auth"
                    )
            if "ice_policy" in meta and meta["ice_policy"] not in ICE_POLICIES:
                raise SchemaValidationError(
                    f"{cls.__name__}: meta.ice_policy must be one of "
                    f"{ICE_POLICIES}, got {meta['ice_policy']!r}"
                )
            if target_type != "mission" and "participant_id" in meta:
                raise SchemaValidationError(
                    f"{cls.__name__}: meta.participant_id is only valid for "
                    "target_type='mission'"
                )

        # Deliberately OUTSIDE the `meta is not None` guard above: a mission
        # grant with no `meta` at all must fail this the same way one with
        # an empty meta does -- participant_id is required, not merely
        # validated-if-present. Same conditional-on-target_type shape as
        # invite_ref's own target_type=='org:join' check below, written
        # against a meta key rather than a typed top-level field: see
        # graph://ce07a01f-faa ("More information") for why a
        # schema_revision bump buys nothing once meta's own validator
        # applies the same rigor.
        if target_type == "mission":
            participant_id = (meta or {}).get("participant_id")
            if not isinstance(participant_id, str) or not participant_id:
                raise SchemaValidationError(
                    f"{cls.__name__}: meta.participant_id is required for "
                    "target_type='mission' (a mission grant is always "
                    "bound to one guest identity)"
                )

        subject = payload.get("subject")
        if not isinstance(subject, dict) or set(subject) != {"kind", "id"}:
            raise SchemaValidationError(
                f"{cls.__name__}: 'subject' must be an object with exactly "
                "{kind, id} (the issuing cert's subject, I6)"
            )
        kind = subject.get("kind")
        if kind not in SUBJECT_KINDS_NETWORK:
            raise SchemaValidationError(
                f"{cls.__name__}: subject.kind must be one of "
                f"{SUBJECT_KINDS_NETWORK}, got {kind!r}"
            )
        if kind in RESERVED_SUBJECT_KINDS:
            raise SchemaValidationError(
                f"{cls.__name__}: subject.kind 'persona' is RESERVED for "
                "rung 2 (Track E session linking); grants are issued by "
                "operator or agent subjects today"
            )
        if not isinstance(subject.get("id"), str) or not subject["id"]:
            raise SchemaValidationError(
                f"{cls.__name__}: subject.id must be a non-empty string"
            )


class NetworkLinkGrantV2(NetworkLinkGrantV1):
    """Current dashboard-side share-link grant.

    ``url`` is the canonical public presentation of the hostname-independent
    token. Consumers request this revision and always receive the complete
    current shape from Settings, regardless of the stored revision.
    """

    set_id = NETWORK_LINK_GRANT_SET_ID
    schema_revision = 2

    url: str = field(
        required=True,
        description=(
            "Public HTTPS URL returned by the issuing registry for this "
            "hostname-independent grant token."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        from urllib.parse import urlsplit

        super().validate(payload)
        if not isinstance(payload, dict):
            return
        url = _require_str(payload, "url", cls.__name__, max_len=512)
        parsed = urlsplit(url)
        expected_path = f"/l/{payload.get('token', '')}"
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path != expected_path
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'url' must be an HTTPS /l/<token> URL "
                "for this grant, without credentials, query, or fragment"
            )

    @classmethod
    def upconvert_from_prev(cls, payload: dict) -> dict:
        current = dict(payload)
        current["url"] = f"{NETWORK_PUBLIC_LINK_BASE_URL}/l/{current['token']}"
        return current


class NetworkLinkGrantV3(NetworkLinkGrantV2):
    """Current share-link grant, including invitation join context."""

    set_id = NETWORK_LINK_GRANT_SET_ID
    schema_revision = NETWORK_LINK_GRANT_REVISION

    invite_ref: str = field(
        required=False,
        description=(
            "The 64-hex event id of the invite admitted by an org:join "
            "link. Present only when target_type is org:join."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        if payload.get("target_type") == "org:join":
            _require_hex(
                payload,
                "invite_ref",
                cls.__name__,
                length=NETWORK_PUB_HEX_LEN,
            )
        elif "invite_ref" in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: 'invite_ref' is only valid for "
                "target_type='org:join'"
            )

    @classmethod
    def upconvert_from_prev(cls, payload: dict) -> dict:
        return dict(payload)


# ── autonomy.network.serve-cert ───────────────────────────────


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@publication_band(min="raw", max="raw")
@home("organization")
@singleton(key="default")
class NetworkServeCertV2(SettingSchema):
    """One serving key with two context-specific root-signed certificates.

    ``cert`` is persona-bearing and used only for registry tunnel admission.
    ``viewer_cert`` is identity-neutral and is the only certificate permitted
    in viewer SERVER_HELLO bytes. Both certify the same fresh child key,
    organization, exact ``tunnel:serve`` scope, and validity window.

    The delegate's private key is DELIBERATELY not stored here. This is an
    unattended managed subprocess: it signs SERVER_HELLO for every viewer
    channel with no human present, so its key must be usable at rest — it
    cannot be a passphrase-sealed armor. Rather than weaken the org-key's I1
    discipline by admitting raw key material into the settings store, the
    serving key lives as a mode-0600 FILE on disk (the standard shape for an
    unattended service key), and this row records only its ``key_path``. New
    rows store a portable basename resolved against the manifest-rooted
    serving-key directory; legacy absolute rows remain readable in place. The
    settings store thus still holds no plaintext key material of any kind; the
    signing secret is a filesystem credential protected by file permissions.
    It is also only a narrow delegate
    (``tunnel:serve`` only), short-lived (30-day TTL, I7), and auto-expiring —
    never the sovereign root, which only the operator's browser ever holds and
    only to MINT this delegate.

    There is no compatibility conversion from the former single-certificate
    shape: a row missing ``viewer_cert`` is simply unusable and the next root
    unlock provisions a fresh credential.
    """

    set_id = NETWORK_SERVE_CERT_SET_ID
    schema_revision = NETWORK_SERVE_CERT_REVISION

    cert: str = field(
        required=True,
        description=(
            "The root-signed tunnel:serve delegation certificate in canonical "
            "idkit wire JSON (tools/network/idkit DelegationCert.to_json). "
            "Persona-bearing and used only for registry tunnel admission."
        ),
    )
    viewer_cert: str = field(
        required=True,
        description=(
            "The identity-neutral root-signed tunnel:serve certificate used "
            "only in viewer SERVER_HELLO bytes. Its subject is exactly "
            "{kind: operator, id: child_pub}."
        ),
    )
    key_path: str = field(
        required=True,
        description=(
            "Portable basename of the mode-0600 file holding the delegate's "
            "Ed25519 signing key (legacy absolute paths remain readable). The "
            "key is a server-side filesystem credential, never settings-store "
            "data; the supervisor resolves it within the manifest-rooted key "
            "directory and verifies it matches the cert's child_pub before "
            "launching the connector."
        ),
    )
    root_pub: str = field(
        required=True,
        description=(
            "The org root public key this delegate chains to (64 lowercase "
            "hex = the key id). The chain is verified against it at write."
        ),
    )
    not_after: int = field(
        required=True,
        description=(
            "Epoch seconds the delegate expires — mirrors the cert's "
            "not_after, so startup/serve paths can check expiry without "
            "parsing the cert."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        cert_wire = _require_str(payload, "cert", cls.__name__, max_len=16384)
        _require_str(payload, "key_path", cls.__name__, max_len=4096)
        root_pub = _require_hex(payload, "root_pub", cls.__name__,
                               length=NETWORK_PUB_HEX_LEN)
        not_after = payload.get("not_after")
        if type(not_after) is not int or not_after <= 0:
            raise SchemaValidationError(
                f"{cls.__name__}: 'not_after' must be a positive epoch-seconds "
                "integer"
            )

        # Fail-closed crypto gate. Lazy import keeps tools.graph importable
        # without `cryptography`; unavailable verifier → no write.
        try:
            from tools.network.idkit import (
                DelegationCert,
                IdkitError,
                verify_chain,
            )
        except Exception as exc:  # pragma: no cover — env without idkit deps
            raise SchemaValidationError(
                f"{cls.__name__}: cannot verify the serve-cert — "
                f"tools.network.idkit is unavailable ({exc}); refusing the "
                "write (fail-closed)"
            ) from exc
        try:
            cert = DelegationCert.from_json(cert_wire)
            viewer_cert = DelegationCert.from_json(
                _require_str(payload, "viewer_cert", cls.__name__, max_len=16384)
            )
        except Exception as e:
            raise SchemaValidationError(
                f"{cls.__name__}: serving certificate does not parse as a delegation "
                f"certificate: {e}"
            ) from e

        if tuple(cert.scope) != (SERVE_CERT_SCOPE,):
            raise SchemaValidationError(
                f"{cls.__name__}: cert scope must be exactly {SERVE_CERT_SCOPE!r}, "
                f"got {list(cert.scope)}"
            )
        if cert.parent_cert is not None:
            raise SchemaValidationError(
                f"{cls.__name__}: registry cert must be issued directly by the org root"
            )
        if (
            cert.subject.kind != "persona"
            or re.fullmatch(r"[0-9a-f]{64}", cert.subject.id) is None
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: cert subject must be a canonical "
                "organization-scoped persona public key"
            )
        if cert.not_after != not_after:
            raise SchemaValidationError(
                f"{cls.__name__}: 'not_after' ({not_after}) does not match the "
                f"cert's not_after ({cert.not_after})"
            )
        if (
            viewer_cert.child_pub != cert.child_pub
            or viewer_cert.org != cert.org
            or tuple(viewer_cert.scope) != tuple(cert.scope)
            or viewer_cert.not_before != cert.not_before
            or viewer_cert.not_after != cert.not_after
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: registry and viewer certificates must name "
                "the same child key, organization, scope, and validity window"
            )
        if viewer_cert.parent_cert is not None:
            raise SchemaValidationError(
                f"{cls.__name__}: viewer cert must be issued directly by the org root"
            )
        if (
            viewer_cert.subject.kind != "operator"
            or viewer_cert.subject.id != viewer_cert.child_pub
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: viewer cert subject must be identity-neutral "
                "{kind: operator, id: child_pub}"
            )

        # Settings validation has no injected clock, so verify both chains at
        # a point inside their common validity window. The HTTP route and
        # supervisor separately enforce current-time validity.
        mid = (cert.not_before + cert.not_after) // 2
        try:
            for candidate in (cert, viewer_cert):
                verified = verify_chain(
                    candidate,
                    root_pub,
                    org=cert.org,
                    now=mid,
                    required_scope=SERVE_CERT_SCOPE,
                )
                if verified.depth != 1:
                    raise IdkitError(
                        "serving delegates must be issued directly by the org root"
                    )
        except IdkitError as e:
            raise SchemaValidationError(
                f"{cls.__name__}: serving cert does not chain to root_pub with "
                f"{SERVE_CERT_SCOPE} scope: {e}"
            ) from e


@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="genesis_id")
class NetworkPersonaV1(SettingSchema):
    """Which persona THIS NODE holds the seed for, in one organization.

    Home: the operator's own database, declared because the write site
    already depends on it (``org_ops._record_persona``: two members'
    personas on one shared org row would each read the other's identity).
    The declaration replaces the ``org=None``-means-personal convention the
    caller relied on before writes without an org were refused.

    Key: the org's genesis event id (64 lowercase hex) — the same value the
    persona was derived under. Not a label like ``default``: org-key uses one
    because "one org may hold more than one network identity", and that
    multiplicity does not exist here. ``derive_persona`` is deterministic, so
    one personal root plus one org yields exactly one persona; keying on the
    derivation input makes the row self-describing and makes a re-found org
    (new genesis, genuinely different persona) a second row rather than a
    silent overwrite of the first.

    Scope: written with ``org=None``, which resolves to ``personal.db`` — this
    operator's own database, NOT the org's shared one. That is the whole point.
    An org DB is shared by its members; this row answers "which persona is
    MINE", a question with a different answer for every reader, and a shared
    store has no "me". Two members writing it under ``org=<slug>`` would
    collide on one (set_id, key, org) row and each would read the other's
    identity — silent misattribution in the record whose purpose is correct
    attribution.

    The complementary fact — "persona P is a member of org O with role R" — is
    shared, and already lives in the org ledger: signed, replicated, and
    merge-defined by folding an event DAG. It is not duplicated here. What the
    ledger cannot say is which of its members is the caller, because that
    depends on who holds which seed. This row says only that, so it needs no
    merge rule, no sync, and no replication: a second node with the same
    personal root re-derives the same persona and writes the same row locally.

    Public halves only. The persona private key is re-derivable from the
    personal root seed whenever a signature is actually needed, so storing it
    would convert a public index into a secret store for no gain.
    """

    set_id = NETWORK_PERSONA_SET_ID
    schema_revision = NETWORK_PERSONA_REVISION

    persona_pub: str = field(
        required=True,
        description=(
            "The persona's Ed25519 PUBLIC key (64 lowercase hex = the key "
            "id): HKDF-SHA256(personal_root_seed, salt=persona-v1, "
            "info=genesis_id). Deterministic within an org and unlinkable "
            "across orgs. This is the stable member id bound at claim time — "
            "NOT the rotatable current signing key, which diverges from it at "
            "the first rekey."
        ),
    )
    derived_at: str = field(
        required=True,
        description="ISO-8601 UTC timestamp of the ceremony that derived it.",
    )
    source: str = field(
        required=True,
        description=(
            "Which ceremony produced the persona: 'found' (this node founded "
            "the org) or 'join' (this node claimed membership under an "
            "invite). Known with certainty because the row is written at that "
            "ceremony, which is the only moment the value exists without a "
            "passphrase."
        ),
    )
    invite_ref: str = field(
        required=False,
        description="The invite this persona claimed under; join only.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _require_hex(payload, "persona_pub", cls.__name__, length=NETWORK_PUB_HEX_LEN)
        _require_iso_ts(payload, "derived_at", cls.__name__)
        source = _require_str(payload, "source", cls.__name__, max_len=16)
        if source not in PERSONA_SOURCES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'source' must be one of "
                f"{list(PERSONA_SOURCES)}, got {source!r}"
            )
        if source == "join" and not payload.get("invite_ref"):
            raise SchemaValidationError(
                f"{cls.__name__}: 'invite_ref' is required when source='join'"
            )
        # A private key here would be a category error, not a typo: the whole
        # design rests on this row being public-only. Reject the shapes a
        # careless caller would reach for rather than trusting review.
        for banned in ("private_hex", "persona_priv", "seed", "personal_root_seed",
                       "armored_private_key", "sealed"):
            if banned in payload:
                raise SchemaValidationError(
                    f"{cls.__name__}: {banned!r} must never be stored — this "
                    "row is public-only; the private half is re-derived from "
                    "the personal root seed when a signature is needed"
                )
