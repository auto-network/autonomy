"""Human-factor release of one secured vault Setting: the vault side.

The ``vault_open`` approval runs on Central (vault_open_central.py). This
module is what it calls into the vault for: freezing one secured Setting and
its policy generation for an authenticated session (:func:`freeze_request`),
the operator's factor ceremony and open bundle (:func:`ceremony_for`), the
frozen-context check (:func:`assert_frozen`), and opening the frozen revision
with the operator's content key into the requester's private ramfs
(:func:`open_and_deliver`). vault_routes also uses :func:`_setting_route`.

No content-encryption key, factor seed, password, armor, or vault locator is
ever placed in the requester-visible request/result.  The one factor gesture
is the authorization; there is no preceding generic approval followed by a
second decrypt dialog.
"""

from __future__ import annotations

import hashlib
import hmac
import time

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


# ttl_seconds is the delivered credential's ramfs LIFETIME (not the approval
# window — that is the kind's FIXED policy). 0 means the FULL CONTAINER
# LIFESPAN: no timed destruction; the file dies with the container's private
# mount. A positive value destroys the file that many seconds after delivery.
MIN_TTL_SECONDS = 0
MAX_TTL_SECONDS = 86400
DEFAULT_TTL_SECONDS = 0
_ALLOWED_REQUEST_FIELDS = {"set_id", "key", "ttl_seconds"}


def _setting_route(
    principal: api_auth.ApiPrincipal,
    set_id: str,
    key: str,
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
            raise ValueError(f"vault_open {exc}") from exc
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


def freeze_request(principal: api_auth.ApiPrincipal, request: dict) -> tuple[dict, dict]:
    """Freeze a release of one secured Setting for an authenticated session.

    ``principal`` is the requesting session, proven by the caller from its
    bearer; nothing in ``request`` names a session, organization or scope.
    Returns the requester-visible request (target, access, requester, delivery
    lifetime) and the staged context. The staged context holds only
    identifiers and digests: the policy snapshot and factor roster are
    re-derived and compared at bootstrap and delivery (:func:`assert_frozen`),
    so no policy generation rides the replicated Central row.
    """
    if principal.kind not in {
        api_auth.ApiPrincipalKind.ORG_SESSION,
        api_auth.ApiPrincipalKind.LOCAL_SESSION,
    } or not principal.subject:
        raise PermissionError("vault_open requires an authenticated session bearer")
    if not isinstance(request, dict) or set(request) - _ALLOWED_REQUEST_FIELDS:
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
            "label": str(launcher.get("label") or principal.subject)[:120],
        },
        "target": f"{set_id}/{routed_key}",
        "delivery": "plaintext Setting value to the requesting session",
        "release_mode": "delivered",
        "access": str(
            (class_snapshot.get("governance") or {}).get("display_name")
            or class_snapshot.get("policy")
        )[:200],
        # ttl_seconds is the delivered credential's ramfs LIFETIME (delivery
        # applies it from delivery time). The pending-approval window is the
        # kind's FIXED policy, not this value — see approval_kind_registry.
        "ttl_seconds": ttl,
    }
    staged = {
        "v": 2,
        "org": member.org,
        "setting_id": member.id,
        "sealed_digest": _digest(sealed),
        "class_id": class_id,
        "gen_id": gen_id,
        "class_digest": _digest(class_snapshot),
        "factor_digest": _digest(ceremony_bootstrap),
    }
    return safe_request, staged


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


def _frozen_snapshot(staged: dict) -> dict:
    """The policy snapshot the request froze, re-derived and digest-checked."""
    snapshot, _record = _class_snapshot(
        staged["class_id"], staged.get("org"), gen_id=staged["gen_id"],
    )
    if not hmac.compare_digest(_digest(snapshot), str(staged.get("class_digest") or "")):
        raise VaultError("the vault policy changed before approval")
    return snapshot


def ceremony_for(request: dict, staged: dict) -> dict:
    """The operator's factor ceremony inputs AND the open bundle.

    The operator's browser opens the policy class locally, so besides the
    factor ceremony it needs the inner-blob inputs: the frozen factor
    ``generation`` (wraps + sealing public key), the sealed CEK, and the
    genesis/setting identifiers the seal binds. Nothing here opens more than
    this one revision: no content key, no opener seed, and no class key ever
    appears. The caller serves it only to the operator, and the generation is
    re-derived from the digest-frozen snapshot so the browser opens exactly
    what the request committed to.
    """
    snapshot = _frozen_snapshot(staged)
    ceremony = _ceremony_bootstrap(snapshot, staged.get("org"))
    if not hmac.compare_digest(
        _digest(ceremony), str(staged.get("factor_digest") or ""),
    ):
        raise VaultError("the vault factors changed before approval")
    setting = request.get("setting") or {}
    bundle = settings_ops.secured_open_bundle(
        setting.get("set_id"),
        setting.get("key"),
        setting_id=staged.get("setting_id"),
        sealed_content_key_digest=staged.get("sealed_digest"),
        org=staged.get("org"),
    )
    if bundle["class_id"] != snapshot.get("class_id"):
        raise VaultError("the vault policy changed before approval")
    bundle["generation"] = snapshot.get("generation")
    bundle["policy"] = snapshot.get("policy")
    return {"ceremony": ceremony, "bundle": bundle}


def assert_frozen(request: dict, staged: dict) -> None:
    """Refuse when the requesting session or the frozen policy changed.

    No wall-clock check here: the request's own expiry and the delivery
    window bound the time. This catches what actually changed: the session's
    workspace binding, the policy snapshot and the factor roster.
    """
    requester = request.get("requester") or {}
    if staged.get("v") != 2 or not isinstance(request.get("setting"), dict):
        raise VaultError("this request has no frozen vault-open context")
    launcher = dashboard_db.get_session(requester.get("session"))
    if launcher is None or str(launcher.get("project") or "").strip() != (
        requester.get("workspace")
    ):
        raise VaultError("the requesting session changed before approval")
    snapshot = _frozen_snapshot(staged)
    ceremony = _ceremony_bootstrap(snapshot, staged.get("org"))
    if not hmac.compare_digest(
        _digest(ceremony), str(staged.get("factor_digest") or ""),
    ):
        raise VaultError("the vault factors changed before approval")


def open_and_deliver(release_id: str, request: dict, staged: dict,
                     content_key: bytearray) -> dict:
    """Open the frozen revision with the operator's CEK and write the value
    only into the requester's private ramfs; return the value-free receipt.

    The CEK opens this one revision and nothing else. The caller owns
    ``content_key`` and zeroes it; the opened payload is cleared here as soon
    as the ramfs writer returns.
    """
    assert_frozen(request, staged)
    payload = settings_ops.open_secured_setting(
        request["setting"]["set_id"],
        request["setting"]["key"],
        setting_id=staged["setting_id"],
        sealed_content_key_digest=staged["sealed_digest"],
        content_key=content_key,
        org=staged.get("org"),
    )
    try:
        row = {
            "id": release_id,
            "session": (request.get("requester") or {}).get("session"),
            "request": request,
        }
        return vault_release_delivery.deliver_payload(row, payload)
    finally:
        # Drop every nested value reference as soon as the ramfs writer
        # returns. Immutable Python strings cannot be overwritten, but no
        # payload object outlives this chokepoint.
        payload.clear()
