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
* ``autonomy.network.serve-cert#1`` — the org's serving delegate: a
  root-signed, short-lived ``tunnel:serve`` certificate plus a POINTER to
  the on-disk (mode-0600) key file the unattended connector subprocess
  signs with. The signing key is a filesystem credential, never
  settings-store data, so I1's no-plaintext-key discipline holds.

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
NETWORK_LINK_GRANT_REVISION = 2
NETWORK_PUBLIC_LINK_BASE_URL = "https://relay.auto.network"
NETWORK_SERVE_CERT_SET_ID = "autonomy.network.serve-cert"
NETWORK_SERVE_CERT_REVISION = 1
SERVE_CERT_SCOPE = "tunnel:serve"


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


class NetworkLinkGrantV2(NetworkLinkGrantV1):
    """Current dashboard-side share-link grant.

    ``url`` is the canonical public presentation of the hostname-independent
    token. Consumers request this revision and always receive the complete
    current shape from Settings, regardless of the stored revision.
    """

    set_id = NETWORK_LINK_GRANT_SET_ID
    schema_revision = NETWORK_LINK_GRANT_REVISION

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


# ── autonomy.network.serve-cert ───────────────────────────────


@keyed_per_entity
class NetworkServeCertV1(SettingSchema):
    """The org's serving delegate for the auto.network tunnel (§5.1).

    Key: an operator-chosen label (e.g. ``default``). Payload: a root-signed
    ``tunnel:serve`` delegation cert AND the delegate's PRIVATE key, both at
    rest server-side.

    The delegate's private key is DELIBERATELY not stored here. This is an
    unattended managed subprocess: it signs SERVER_HELLO for every viewer
    channel with no human present, so its key must be usable at rest — it
    cannot be a passphrase-sealed armor. Rather than weaken the org-key's I1
    discipline by admitting raw key material into the settings store, the
    serving key lives as a mode-0600 FILE on disk (the standard shape for an
    unattended service key), and this row records only its ``key_path``. The
    settings store thus still holds no plaintext key material of any kind; the
    signing secret is a filesystem credential, protected by file permissions,
    exactly like a TLS or SSH service key. It is also only a narrow delegate
    (``tunnel:serve`` only), short-lived (30-day TTL, I7), and auto-expiring —
    never the sovereign root, which only the operator's browser ever holds and
    only to MINT this delegate.

    The validator is a fail-closed crypto gate: the ``cert`` must be a genuine
    root-signed ``tunnel:serve`` delegation whose chain verifies to the
    declared root, or it does not enter the store. The key file itself is
    verified to match ``cert.child_pub`` by the supervisor before it launches
    the connector — the layer that actually holds the key.
    """

    set_id = NETWORK_SERVE_CERT_SET_ID
    schema_revision = NETWORK_SERVE_CERT_REVISION

    cert: str = field(
        required=True,
        description=(
            "The root-signed tunnel:serve delegation certificate in canonical "
            "idkit wire JSON (tools/network/idkit DelegationCert.to_json). "
            "Public by nature — it is sent in the clear in every SERVER_HELLO."
        ),
    )
    key_path: str = field(
        required=True,
        description=(
            "Filesystem path to the mode-0600 file holding the delegate's "
            "Ed25519 signing key. The key is a server-side filesystem "
            "credential, never settings-store data; the supervisor verifies "
            "it matches the cert's child_pub before launching the connector."
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
        except Exception as e:
            raise SchemaValidationError(
                f"{cls.__name__}: 'cert' does not parse as a delegation "
                f"certificate: {e}"
            ) from e
        if SERVE_CERT_SCOPE not in cert.scope:
            raise SchemaValidationError(
                f"{cls.__name__}: cert scope must include {SERVE_CERT_SCOPE!r}, "
                f"got {list(cert.scope)}"
            )
        if cert.not_after != not_after:
            raise SchemaValidationError(
                f"{cls.__name__}: 'not_after' ({not_after}) does not match the "
                f"cert's not_after ({cert.not_after})"
            )
        # The chain must verify to the declared root with tunnel:serve, at a
        # time inside the validity window (mid-window avoids boundary edges).
        mid = (cert.not_before + cert.not_after) // 2
        try:
            verify_chain(cert, root_pub, org=cert.org, now=mid,
                         required_scope=SERVE_CERT_SCOPE)
        except IdkitError as e:
            raise SchemaValidationError(
                f"{cls.__name__}: cert does not chain to root_pub with "
                f"{SERVE_CERT_SCOPE} scope: {e}"
            ) from e
