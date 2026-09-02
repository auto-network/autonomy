"""Human-factor release of one secured vault Setting.

``vault_open`` is a kind on the generic approval rendezvous.  The requesting
agent names only a secured set member and a short delivery TTL.  This module
derives the real session/workspace from its bearer, freezes the selected
Setting and policy generation, serves factor bootstrap material only to the
operator-cookie browser, and writes the opened Setting payload only into the
requesting session's isolated memory-backed delivery directory.  The approval
result carries the value-free delivery receipt.

No content-encryption key, factor seed, password, armor, or vault locator is
ever placed in the requester-visible request/result.  The one factor gesture
is the authorization; there is no preceding generic approval followed by a
second decrypt dialog.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from starlette.requests import Request

from tools.dashboard import api_auth
from tools.dashboard import vault_release_delivery
from tools.dashboard.identity_routes import _passkey_rows, _personal_member
from tools.dashboard.dao import dashboard_db
from tools.graph import schemas, settings_ops
from tools.graph.schemas.personal_identity import PASSKEY_SET_ID
from tools.graph.schemas.vault_policy_class import VAULT_POLICY_CLASS_SET_ID
from tools.network.idkit.root_factor_policy import (
    parse_armored_envelope,
    policy_satisfied,
)
from tools.network.idkit.canonical import canonical_json
from tools.vault.errors import VaultError
from tools.vault.key_holder import _scoped_db
from tools.vault.store import VaultStore
from tools.vault.policy_class import ROOT_REACHABLE_FORM
from tools.vault.recipients import PERSONAL_ROOT_RECIPIENT


KIND = "vault_open"
# ttl_seconds is the delivered credential's ramfs LIFETIME (not the approval
# window — that is the kind's FIXED policy). 0 means the FULL CONTAINER
# LIFESPAN: no timed destruction; the file dies with the container's private
# mount. A positive value destroys the file that many seconds after delivery.
MIN_TTL_SECONDS = 0
MAX_TTL_SECONDS = 86400
DEFAULT_TTL_SECONDS = 0
_ALLOWED_REQUEST_FIELDS = {"set_id", "key", "ttl_seconds"}
_HEX_SEED_LEN = 64


def _setting_route(
    principal: api_auth.ApiPrincipal,
    set_id: str,
    key: str,
    *,
    op: str = KIND,
) -> tuple[str, str | None]:
    """Resolve a request suffix to its exact store key and database scope.

    Organization bearers never address the personal store directly.  A
    personal-homed schema may opt into this one mediated shape, in which the
    request carries a suffix and the server derives the bearer organization
    prefix.  The same derivation is used by the seal route.
    """
    if (
        principal.kind is api_auth.ApiPrincipalKind.ORG_SESSION
        and principal.org != "personal"
        and schemas.declared_home(set_id) == "personal"
    ):
        if (
            schemas.declared_org_writeback_key_strategy(set_id)
            != "org_slug:credential_name"
        ):
            raise PermissionError(
                "this personal secured set has no organization-keyed release namespace"
            )
        try:
            return schemas.derive_org_writeback_key(
                set_id, principal.org or "", key,
            ), None
        except ValueError as exc:
            raise ValueError(f"{op} {exc}") from exc
    return key, principal.org

def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _class_snapshot(
    class_id: str,
    org: str | None,
    *,
    gen_id: str | None = None,
) -> tuple[dict, object]:
    with VaultStore(_scoped_db(VAULT_POLICY_CLASS_SET_ID, org)) as store:
        record = store.get_class(class_id)
    # A secured Setting may name an older generation. Its factor roster must
    # come from that exact generation—the same one ``open_cek`` will use—not
    # from today's generation after a revocation or enrollment.
    generation = record.generation(gen_id) if gen_id else record.current()
    snapshot = {
        "class_id": record.class_id,
        "policy": record.policy,
        "generation": generation.to_dict(),
    }
    if record.governance is not None:
        snapshot["governance"] = record.governance
    return snapshot, record


def _ceremony_bootstrap(class_snapshot: dict, org: str | None) -> dict:
    """Resolve the browser inputs for the frozen generation.

    The returned list is digest-frozen at request creation and re-derived at
    enrichment and execution.  Armor remains confined to server staging and
    the operator-cookie response; it is never copied into the safe request.
    """
    generation = class_snapshot.get("generation") or {}
    governance = class_snapshot.get("governance")
    if isinstance(governance, dict) and governance.get("form") == ROOT_REACHABLE_FORM:
        anchor_id = governance.get("anchor_id")
        with VaultStore(_scoped_db(VAULT_POLICY_CLASS_SET_ID, org)) as store:
            anchor = store.get_root_anchor(anchor_id)
        wraps = generation.get("wraps") or []
        if (
            len(wraps) != 1
            or wraps[0].get("factor_id") != anchor.anchor_id
            or wraps[0].get("factor_type") != PERSONAL_ROOT_RECIPIENT
            or wraps[0].get("public_key") != anchor.public_key
        ):
            raise VaultError("root-reachable class and personal anchor disagree")
        personal = _personal_member()
        if personal is None or not isinstance(personal.payload, dict):
            raise VaultError("no personal root is enrolled")
        if personal.payload.get("root_pub") != anchor.root_pub:
            raise VaultError("the vault anchor is not carried by the current personal root")
        armor = personal.payload.get("armored_private_key")
        if not isinstance(armor, str) or not armor:
            raise VaultError("the personal root has no canonical armor")
        # A v3 root-factor-policy armor: factors + AND/OR policy in the
        # envelope. The browser opens it with openFactorPolicyArmor; here we
        # freeze the passkey roster and which opener methods can satisfy the
        # policy (password alone, passkey alone, or one of each).
        try:
            envelope = parse_armored_envelope(armor)
        except Exception as exc:
            raise VaultError(
                "the personal root armor is not a root-factor-policy armor"
            ) from exc
        if envelope.get("root_pub") != anchor.root_pub:
            raise VaultError("the vault anchor is not carried by the current personal root")
        types = {f["factor_id"]: f["type"] for f in envelope["factors"]}
        root_credential_ids = {
            f.get("credential_id")
            for f in envelope["factors"]
            if f.get("type") == "passkey" and isinstance(f.get("credential_id"), str)
        }
        passkeys = []
        for member in _passkey_rows():
            payload = member.payload if isinstance(member.payload, dict) else {}
            if payload.get("credential_id") not in root_credential_ids:
                continue
            passkeys.append({
                "credential_id": payload.get("credential_id"),
                "label": payload.get("label"),
                "rp_id": payload.get("rp_id"),
                "transports": payload.get("transports") or [],
            })
        pw_ids = [fid for fid, t in types.items() if t == "password"]
        pk_ids = [fid for fid, t in types.items() if t == "passkey"]
        methods = []
        if any(policy_satisfied(envelope["policy"], [fid]) for fid in pw_ids):
            methods.append("password")
        if any(policy_satisfied(envelope["policy"], [fid]) for fid in pk_ids):
            methods.append("passkey")
        if not methods and any(
            policy_satisfied(envelope["policy"], [p, k])
            for p in pw_ids for k in pk_ids
        ):
            methods.append("both")
        if not methods:
            raise VaultError("the personal root has no interactive opener")
        if any(m in {"passkey", "both"} for m in methods) and not passkeys:
            raise VaultError("the personal root passkey has no enrolled credential")
        return {
            "v": 2,
            "governance": governance,
            "anchor": anchor.to_dict(),
            "root": {
                "armor": armor,
                "armor_version": 3,
                "root_pub": anchor.root_pub,
                "require_pair": False,
                "passkeys": passkeys,
                "methods": methods,
            },
        }
    factors = []
    seen: set[str] = set()
    with VaultStore(_scoped_db(VAULT_POLICY_CLASS_SET_ID, org)) as store:
        for wrap in generation.get("wraps") or []:
            factor_id = wrap.get("factor_id")
            factor_type = wrap.get("factor_type")
            if not isinstance(factor_id, str) or factor_id in seen:
                continue
            seen.add(factor_id)
            if factor_type == PERSONAL_ROOT_RECIPIENT:
                # The widen-only anchor wrap: opened by the root ceremony, not
                # an interactive factor of this sheet.
                continue
            factor = {"factor_id": factor_id, "type": factor_type}
            if factor_type == "password":
                factor["armor"] = store.get_password_armor(factor_id)
            elif factor_type == "passkey":
                passkey = _passkey_for_public_key(str(wrap.get("public_key") or ""))
                if (
                    not passkey
                    or not isinstance(passkey.get("credential_id"), str)
                    or not passkey["credential_id"]
                    or not isinstance(passkey.get("rp_id"), str)
                    or not passkey["rp_id"]
                ):
                    raise VaultError(
                        f"passkey factor {factor_id!r} has no enrolled credential"
                    )
                factor.update(passkey)
            else:
                raise VaultError(f"unsupported vault factor type {factor_type!r}")
            factors.append(factor)
    if not factors:
        raise VaultError("the policy class has no usable factors")
    return {
        "v": 1,
        "policy": class_snapshot.get("policy"),
        "factors": factors,
    }


def prepare_create_from_request(
    http_request: Request,
    _claimed_session: object,
    request: dict,
) -> tuple[str, dict, dict]:
    """Freeze a request under the authenticated launcher's identity.

    The caller-supplied ``session`` is deliberately ignored.  A bearer may
    request release only for the dashboard launcher record that minted it.
    """
    principal = api_auth.principal_from_request(http_request)
    if principal.kind not in {
        api_auth.ApiPrincipalKind.ORG_SESSION,
        api_auth.ApiPrincipalKind.LOCAL_SESSION,
    } or not principal.subject:
        raise PermissionError("vault_open requires an authenticated session bearer")
    if set(request) - _ALLOWED_REQUEST_FIELDS:
        raise ValueError(
            "vault_open request accepts only set_id, key, and ttl_seconds"
        )
    set_id = request.get("set_id")
    key = request.get("key")
    if not isinstance(set_id, str) or not set_id or len(set_id) > 256:
        raise ValueError("vault_open set_id must be a short non-empty string")
    if not isinstance(key, str) or not key or len(key) > 256:
        raise ValueError("vault_open key must be a short non-empty string")
    if schemas.declared_vault_tier(set_id) != "secured":
        raise ValueError(f"{set_id!r} is not a secured vault set")
    ttl = request.get("ttl_seconds", DEFAULT_TTL_SECONDS)
    if isinstance(ttl, bool) or not isinstance(ttl, int) \
            or not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
        raise ValueError(
            f"vault_open ttl_seconds must be {MIN_TTL_SECONDS}–{MAX_TTL_SECONDS}"
        )

    launcher = dashboard_db.get_session(principal.subject)
    if launcher is None:
        raise PermissionError("the authenticated session has no launcher record")
    workspace = str(launcher.get("project") or "").strip()
    if not workspace:
        raise PermissionError("the authenticated session has no workspace binding")

    routed_key, read_org = _setting_route(principal, set_id, key)
    members = settings_ops.read_set(set_id, org=read_org, peers=[]).members
    member = next(
        (candidate for candidate in members if candidate.key == routed_key), None,
    )
    if member is None:
        raise ValueError(f"no secured Setting matches {set_id}/{routed_key}")
    if member.vault_error is not None:
        raise ValueError(member.vault_error.message)
    sealed = member.sealed_content_key
    if not isinstance(sealed, dict):
        raise ValueError(
            f"{set_id}/{routed_key} is not awaiting a human-factor open"
        )
    class_id = sealed.get("policy_class_id")
    if not isinstance(class_id, str) or not class_id:
        raise ValueError("the secured Setting names no policy class")
    sealed_cek = sealed.get("sealed_cek") or {}
    gen_id = sealed_cek.get("gen_id")
    if not isinstance(gen_id, str) or not gen_id:
        raise ValueError("the secured Setting names no policy generation")
    class_snapshot, policy_class = _class_snapshot(
        class_id, member.org, gen_id=gen_id,
    )
    required_policy = sealed.get("required_policy")
    if policy_class.policy != required_policy:
        raise ValueError("the secured Setting and policy class disagree")
    ceremony_bootstrap = _ceremony_bootstrap(class_snapshot, member.org)

    safe_request = {
        "setting": {"set_id": set_id, "key": routed_key, "id": member.id},
        "operation": "read",
        "requester": {
            "session": principal.subject,
            "organization": principal.org or "local",
            "workspace": workspace,
            "label": str(launcher.get("label") or principal.subject),
        },
        "target": f"{set_id}/{routed_key}",
        "delivery": "plaintext Setting value to the requesting session",
        "release_mode": "delivered",
        "access": (
            (class_snapshot.get("governance") or {}).get("display_name")
            or class_snapshot.get("policy")
        ),
        # ttl_seconds is the delivered credential's ramfs LIFETIME (delivery
        # applies it from delivery time). The pending-approval window is the
        # kind's FIXED policy, not this value — see approval_kind_registry.
        "ttl_seconds": ttl,
    }
    staged = {
        "v": 1,
        "org": member.org,
        "setting_id": member.id,
        "sealed_digest": _digest(sealed),
        "class_snapshot": class_snapshot,
        "class_digest": _digest(class_snapshot),
        "factor_digest": _digest(ceremony_bootstrap),
    }
    return principal.subject, safe_request, staged


def _passkey_for_public_key(public_key: str) -> dict | None:
    rows = settings_ops.read_owned_set(PASSKEY_SET_ID, org=None).members
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        statement = payload.get("statement") or {}
        if hmac.compare_digest(
            str(statement.get("provisioning_public_key") or ""), public_key,
        ):
            return {
                "credential_id": payload.get("credential_id"),
                "label": payload.get("label"),
                "rp_id": payload.get("rp_id"),
                "transports": payload.get("transports") or [],
            }
    return None


def enrich_from_request(http_request: Request, row: dict) -> dict:
    """Return factor armor/PRF bootstrap only to the operator browser."""
    principal = api_auth.principal_from_request(http_request)
    if principal.kind is not api_auth.ApiPrincipalKind.OPERATOR_COOKIE:
        raise PermissionError("operator-cookie authority is required for vault factors")
    staged = row.get("staged") or {}
    snapshot = staged.get("class_snapshot") or {}
    ceremony = _ceremony_bootstrap(snapshot, staged.get("org"))
    if not hmac.compare_digest(
        _digest(ceremony), str(staged.get("factor_digest") or ""),
    ):
        raise VaultError("the vault factors changed before approval")
    return {"ceremony": ceremony}


def authorize_decision(
    http_request: Request,
    row: dict,
    decision: dict,
) -> str | None:
    """The factor-bearing decision is accepted only from the operator cookie."""
    principal = api_auth.principal_from_request(http_request)
    if principal.kind is not api_auth.ApiPrincipalKind.OPERATOR_COOKIE:
        return "operator-cookie authority is required to open a secured Setting"
    if decision.get("approved") is False:
        return None if set(decision) == {"approved"} else (
            "a declined vault_open decision must carry only approved"
        )
    if set(decision) != {"approved", "openers"}:
        return "vault_open approval must carry only approved and openers"
    openers = decision.get("openers")
    if not isinstance(openers, dict) or not openers:
        return "vault_open approval requires factor opener seeds"
    if any(
        not isinstance(factor_id, str)
        or not isinstance(seed, str)
        or len(seed) != _HEX_SEED_LEN
        or any(ch not in "0123456789abcdef" for ch in seed)
        for factor_id, seed in openers.items()
    ):
        return "vault_open opener seeds must be 32-byte lowercase hex"
    snapshot = (row.get("staged") or {}).get("class_snapshot") or {}
    wraps = (snapshot.get("generation") or {}).get("wraps") or []
    factor_types = {
        wrap.get("factor_id"): wrap.get("factor_type")
        for wrap in wraps
        if isinstance(wrap, dict)
    }
    if not set(openers).issubset(factor_types):
        return "vault_open contains an opener outside the frozen factor roster"
    governance = snapshot.get("governance")
    if isinstance(governance, dict) and governance.get("form") == ROOT_REACHABLE_FORM:
        anchor_id = governance.get("anchor_id")
        if set(openers) != {anchor_id}:
            return "vault_open requires the frozen personal-root anchor opener"
        if factor_types.get(anchor_id) != PERSONAL_ROOT_RECIPIENT:
            return "vault_open personal-root anchor does not match the class"
        return None

    supplied_types = [factor_types[factor_id] for factor_id in openers]
    policy = snapshot.get("policy")
    satisfied = (
        (policy == "password" and supplied_types == ["password"])
        or (policy == "prf" and supplied_types == ["passkey"])
        or (
            policy == "both"
            and sorted(supplied_types) == ["passkey", "password"]
        )
    )
    if not satisfied:
        return "vault_open requires one complete opener set for its frozen policy"
    return None


def authorize_get(
    http_request: Request,
    row: dict,
    waiting: bool,
) -> str | None:
    """Bind review to the operator and delivery to the exact requester."""
    principal = api_auth.principal_from_request(http_request)
    if waiting:
        if principal.kind not in {
            api_auth.ApiPrincipalKind.ORG_SESSION,
            api_auth.ApiPrincipalKind.LOCAL_SESSION,
        } or principal.subject != row.get("session"):
            return "this vault release belongs to another requesting session"
        return None
    if principal.kind is not api_auth.ApiPrincipalKind.OPERATOR_COOKIE:
        return "operator-cookie authority is required to review vault factors"
    return None


def _assert_frozen(row: dict) -> tuple[dict, dict]:
    req = row.get("request") or {}
    staged = row.get("staged") or {}
    if staged.get("v") != 1 or not isinstance(req.get("setting"), dict):
        raise VaultError("this request has no frozen vault-open context")
    # No wall-clock expiry here: the operator's approval IS the
    # authorization, and the frozen-context checks below (policy snapshot,
    # factor digest, unchanged requesting session) catch anything that
    # actually changed. A time budget that starts at REQUEST creation only
    # rejected approvals the operator was slow to tap — punishing the human
    # for the ceremony, never closing a real hole.
    launcher = dashboard_db.get_session(row.get("session"))
    requester = req.get("requester") or {}
    if launcher is None or str(launcher.get("project") or "").strip() != (
        requester.get("workspace")
    ):
        raise VaultError("the requesting session changed before approval")
    snapshot, _record = _class_snapshot(
        staged["class_snapshot"]["class_id"],
        staged.get("org"),
        gen_id=staged["class_snapshot"]["generation"]["gen_id"],
    )
    if not hmac.compare_digest(_digest(snapshot), str(staged.get("class_digest") or "")):
        raise VaultError("the vault policy changed before approval")
    ceremony = _ceremony_bootstrap(snapshot, staged.get("org"))
    if not hmac.compare_digest(
        _digest(ceremony), str(staged.get("factor_digest") or ""),
    ):
        raise VaultError("the vault factors changed before approval")
    return req, staged


async def execute(row: dict, decision: dict) -> dict:
    """Open at the chokepoint and materialise only in requester ramfs."""
    req, staged = _assert_frozen(row)
    raw_openers = decision.get("openers") or {}
    opener_buffers: dict[str, bytearray] = {}
    try:
        for factor_id, seed_hex in raw_openers.items():
            opener_buffers[factor_id] = bytearray.fromhex(seed_hex)
        payload = settings_ops.open_secured_setting(
            req["setting"]["set_id"],
            req["setting"]["key"],
            setting_id=staged["setting_id"],
            sealed_content_key_digest=staged["sealed_digest"],
            opener_seeds=opener_buffers,
            org=staged.get("org"),
        )
        try:
            receipt = vault_release_delivery.deliver_payload(row, payload)
            return {"ok": True, "receipt": receipt}
        finally:
            # Drop every nested value reference as soon as the ramfs writer
            # returns. Immutable Python strings cannot be overwritten, but the
            # executor retains no payload object after this chokepoint.
            payload.clear()
    finally:
        for seed in opener_buffers.values():
            seed[:] = b"\x00" * len(seed)
        # The parsed JSON body otherwise remains captured by the executor task
        # until it exits.  Replace the immutable hex strings at the earliest
        # possible boundary; approval result construction drops this field.
        if isinstance(raw_openers, dict):
            for factor_id in list(raw_openers):
                raw_openers[factor_id] = ""


def result(_row: dict, decision: dict, outcome: dict) -> dict:
    """Persist only the value-free ramfs receipt and execution outcome."""
    return {"approved": bool(decision.get("approved")), "execution": outcome}


PREPARE_CREATE_FROM_REQUEST = {KIND: prepare_create_from_request}
ENRICH_FROM_REQUEST = {KIND: enrich_from_request}
AUTHORIZE_DECISION = {KIND: authorize_decision}
AUTHORIZE_GET = {KIND: authorize_get}
EXECUTORS = {KIND: execute}
RESULT_BUILDERS = {KIND: result}
WAIT_RESULT_BUILDERS = {}
