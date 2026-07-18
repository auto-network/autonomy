"""auto.network identity Settings — org key, binding, link-grant cache.

Three contracts back the dashboard side of the auto.network share-link
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
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
)


NETWORK_ORG_KEY_SET_ID = "autonomy.network.org-key"
NETWORK_ORG_KEY_REVISION = 1
NETWORK_BINDING_SET_ID = "autonomy.network.binding"
NETWORK_BINDING_REVISION = 1
NETWORK_LINK_GRANT_SET_ID = "autonomy.network.link-grant"
NETWORK_LINK_GRANT_REVISION = 1


# ── A1 vocabulary pins (tools/network/idkit) ─────────────────
#
# Duplicated (not imported) so tools.graph never pulls the `cryptography`
# dependency at import time; a test asserts these match idkit's exports.

SUBJECT_KINDS_NETWORK = ("agent", "operator", "persona")
RESERVED_SUBJECT_KINDS = ("persona",)  # rung 2: session linking (Track E)
NETWORK_TOKEN_HEX_LEN = 32  # 128-bit CSPRNG token, lowercase hex (I2)
NETWORK_PUB_HEX_LEN = 64  # raw Ed25519 public key, lowercase hex (= key id)

TARGET_TYPES = ("design", "file", "note", "present")  # §6.1 artifact resolver

RECOVERY_MODES = ("none", "recovery-key", "org-vouch", "blindhash-escrow")
RESERVED_RECOVERY_MODES = ("org-vouch", "blindhash-escrow")  # companion ledger spec

GRANT_META_KEYS = frozenset({"ttl", "label", "require_auth"})

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
        "reads this)."
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


@keyed_per_entity
class NetworkOrgKeyV1(SettingSchema):
    """The org's auto.network root key — encrypted armor only (I1).

    Key: an operator-chosen label (e.g. ``default``) — one org may hold
    more than one network identity, mirroring the commit signing-key
    pattern. Payload: the armored, passphrase-encrypted Ed25519 root
    private key. The plaintext key exists only in the operator's browser
    during ceremonies; nothing stored here is usable without the
    passphrase.
    """

    set_id = NETWORK_ORG_KEY_SET_ID
    schema_revision = NETWORK_ORG_KEY_REVISION

    armored_private_key: str = field(
        required=True,
        description=(
            "The armored, passphrase-encrypted Ed25519 org root private key "
            "in the CANONICAL idkit byte form (tools/network/idkit/armor.py "
            "canonicalize_armor). Encrypted at rest; only ever decrypted in "
            "the operator's browser with the passphrase, which the server "
            "never sees (I1)."
        ),
    )
    root_pub: str = field(
        required=False,
        description=(
            "Hex-encoded public half (64 lowercase hex chars = the key id). "
            "Public by definition; lets tooling match this blob to its "
            "registry binding without decrypting anything."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        armor = _require_str(payload, "armored_private_key", cls.__name__, max_len=16384)
        # I1 tripwire: a raw Ed25519 private key is exactly 64 hex chars.
        # Anything that parses as one is plaintext key material, not an
        # encrypted armor — refuse it loudly (kept for the clearer message;
        # the canonical check below refuses it too).
        if len(armor) == 2 * 32 and _HEX_RE.match(armor):
            raise SchemaValidationError(
                f"{cls.__name__}: 'armored_private_key' looks like a raw hex "
                "Ed25519 private key — plaintext key material must never be "
                "stored (I1); store the passphrase-encrypted armor instead"
            )

        # THE I1 gate, at the layer every write path shares. A hardened
        # HTTP route is not enough: settings_ops.add_setting and
        # POST /api/graph/setting reach this schema directly, so the
        # strict/canonical armor requirement must live here. The armor
        # must be EXACTLY the canonical idkit byte form — strict parse
        # (exact field sets, formats, lengths, no duplicate keys) plus
        # byte-for-byte equality with its own re-serialization, so no
        # unknown field, smuggled plaintext, or alternate encoding can
        # ride into storage through ANY path. Import is deliberately
        # lazy (tools.graph stays importable without `cryptography`) and
        # FAIL-CLOSED: no verifier available → no write.
        try:
            from tools.network.idkit.armor import (
                ArmorError,
                canonicalize_armor,
                parse_armor,
            )
        except Exception as exc:  # pragma: no cover — env without idkit deps
            raise SchemaValidationError(
                f"{cls.__name__}: cannot verify 'armored_private_key' — "
                f"tools.network.idkit is unavailable ({exc}); refusing the "
                "write (I1 fail-closed)"
            ) from exc
        try:
            armor_data = parse_armor(armor)
            if canonicalize_armor(armor) != armor:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'armored_private_key' must be the "
                    "canonical armor byte form — re-emit it with "
                    "tools.network.idkit.armor.canonicalize_armor (I1)"
                )
        except ArmorError as e:
            raise SchemaValidationError(
                f"{cls.__name__}: 'armored_private_key' is not a canonical "
                f"passphrase-encrypted org key armor (I1 — plaintext key "
                f"material must never be stored): {e}"
            ) from e

        if "root_pub" in payload:
            _require_hex(payload, "root_pub", cls.__name__, length=NETWORK_PUB_HEX_LEN)
            if payload["root_pub"] != armor_data["root_pub"]:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'root_pub' does not match the armor's "
                    "enclosed public key"
                )


# ── autonomy.network.binding ──────────────────────────────────


@keyed_per_entity
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


@keyed_per_entity
class NetworkLinkGrantV1(SettingSchema):
    """Dashboard-side share-link grant cache (§4.4, §6.1; feeds I9).

    Key: the grant token itself — the same 32-hex string the registry
    minted, so the serving path's I9 check is one keyed read. The token is
    pure CSPRNG output, never derived from the target (I2 — enforced by
    construction in ``tools/network/idkit``); this row is where the
    token→target mapping lives on the dashboard side.
    """

    set_id = NETWORK_LINK_GRANT_SET_ID
    schema_revision = NETWORK_LINK_GRANT_REVISION

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
            "bool}. require_auth=true is RESERVED for rung 2 (Track E "
            "session linking; registry answers 501 until then) and is "
            "rejected for now."
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
