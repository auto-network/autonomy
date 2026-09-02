"""Operator-authenticated HTTP surface for the password vault engine.

The route layer deliberately has no database-path argument.  It resolves the
personal database through the same declared-home resolver as settings and
content, then opens a short-lived :class:`VaultStore` for each request.
Passwords never cross this boundary; the browser submits canonical armor and
ephemeral opener seeds when an operation needs to open a class.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import api_auth
from tools.vault import service
from tools.vault.errors import VaultError
from tools.vault.store import VaultStore
from tools.vault.key_holder import _scoped_db
from tools.vault.policy_class import extend_class
from tools.vault.root_anchor import RootAnchorRecord
from tools.network.idkit.armor import ArmorError
from tools.dashboard.identity_routes import _personal_member
from tools.graph import settings_ops
from tools.graph.schemas import registry as schema_registry
from tools.graph.schemas.registry import SchemaValidationError
from tools.graph.schemas.vault_credential import (
    VAULT_AUDITED_SET_ID,
    VAULT_CREDENTIAL_REVISION,
    VAULT_SECURED_SET_ID,
)

_HOME_SET = "autonomy.identity.personal"
logger = logging.getLogger(__name__)


def _store():
    return VaultStore(_scoped_db(_HOME_SET, None))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guard(request):
    return api_auth.require_global_api_authority(request)


async def factors(request: Request):
    if (denied := _guard(request)) is not None:
        return denied
    with _store() as store:
        return JSONResponse({
            "factors": [f.__dict__ for f in store.factors()],
            "classes": [store.get_class(i).to_dict() for i in store.class_ids()],
            "secrets": store.secret_names(),
        })


def _personal_root_pub() -> str:
    member = _personal_member()
    if member is None or not isinstance(member.payload, dict):
        raise VaultError("no personal root is enrolled")
    root_pub = member.payload.get("root_pub")
    if not isinstance(root_pub, str) or len(root_pub) != 64:
        raise VaultError("the personal root has no canonical public key")
    return root_pub


async def root_anchors(request: Request):
    """Inventory the stable personal recipients and their bound classes."""
    if (denied := _guard(request)) is not None:
        return denied
    root_pub = _personal_root_pub()
    with _store() as store:
        anchors = [
            anchor.to_dict()
            for anchor in (store.get_root_anchor(i) for i in store.root_anchor_ids())
            if anchor.root_pub == root_pub
        ]
        anchor_ids = {anchor["anchor_id"] for anchor in anchors}
        classes = [
            record.to_dict()
            for record in (store.get_class(i) for i in store.class_ids())
            if record.governance
            and record.governance.get("form") == "root-reachable"
            and record.governance.get("anchor_id") in anchor_ids
        ]
    return JSONResponse({"anchors": anchors, "classes": classes})


async def enroll_root_anchor(request: Request):
    """Persist a browser-created anchor only after its root signature verifies."""
    if (denied := _guard(request)) is not None:
        return denied
    try:
        body = await request.json()
        if set(body) != {"anchor"}:
            raise VaultError("root anchor enrollment accepts only anchor")
        anchor = RootAnchorRecord.from_dict(body["anchor"])
        if anchor.root_pub != _personal_root_pub():
            raise VaultError("root anchor is signed by a different personal root")
        with _store() as store:
            service.enroll_root_anchor(store, anchor.to_dict())
        return JSONResponse({"anchor": anchor.to_dict()}, status_code=201)
    except (KeyError, TypeError, ValueError, VaultError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def create_root_class(request: Request):
    """Mint a class to an enrolled anchor's public recipient (no opener)."""
    if (denied := _guard(request)) is not None:
        return denied
    try:
        body = await request.json()
        if set(body) != {"display_name"}:
            raise VaultError("root class creation accepts only display_name")
        display_name = body.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            raise VaultError("root class display_name must be non-empty")
        with _store() as store:
            anchor = store.get_root_anchor(request.path_params["anchor_id"])
            if anchor.root_pub != _personal_root_pub():
                raise VaultError("root anchor belongs to a different personal root")
            before = set(store.class_ids())
            class_id = service.ensure_root_policy_class(
                store,
                anchor.anchor_id,
                display_name=display_name.strip(),
                created_at=_now(),
            )
            record = store.get_class(class_id)
        return JSONResponse(
            {"policy_class": record.to_dict()},
            status_code=200 if class_id in before else 201,
        )
    except (KeyError, TypeError, ValueError, VaultError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def seal_personal_setting(request: Request):
    """Route one submitted secret into the personal secured store.

    This is the narrow W2 application-routing seam, not a vault write ACL.
    An authenticated session bearer is accepted so ``graph set seal`` can
    submit personal-destined material without widening the generic Settings
    API across stores. The bearer is attribution only: this handler fixes the
    schema, publication state, and personal destination, then public-key seals
    immediately. Operator-cookie calls use the identical seam.
    """
    if (denied := api_auth.require_authenticated_api_caller(request)) is not None:
        return denied
    try:
        body = await request.json()
        if set(body) != {"key", "value", "policy_class_id"}:
            raise VaultError(
                "personal secured writes accept only key, value, and policy_class_id"
            )
        key = body.get("key")
        value = body.get("value")
        class_id = body.get("policy_class_id")
        if not isinstance(key, str) or not key.strip() or len(key) > 256:
            raise VaultError("credential key must be a short non-empty string")
        if not isinstance(value, str) or not value:
            raise VaultError("credential value must be a non-empty string")
        if not isinstance(class_id, str) or not class_id:
            raise VaultError("policy_class_id must be a non-empty string")
        principal = api_auth.principal_from_request(request)
        if principal.kind in {
            api_auth.ApiPrincipalKind.OPERATOR_COOKIE,
            api_auth.ApiPrincipalKind.LOCAL_SESSION,
        } or (
            principal.kind is api_auth.ApiPrincipalKind.ORG_SESSION
            and principal.org == "personal"
        ):
            routed_key = key.strip()
        elif principal.kind is api_auth.ApiPrincipalKind.ORG_SESSION:
            if not principal.org:
                return JSONResponse(
                    {"error": "organization session has no trusted organization"},
                    status_code=403,
                )
            if (
                schema_registry.declared_home(VAULT_SECURED_SET_ID) != "personal"
                or schema_registry.declared_org_writeback_key_strategy(
                    VAULT_SECURED_SET_ID
                )
                != "org_slug:credential_name"
            ):
                logger.warning(
                    "personal_vault_write_refused caller=%s caller_org=%s "
                    "reason=set-not-org-keyed set_id=%s",
                    principal.subject,
                    principal.org,
                    VAULT_SECURED_SET_ID,
                )
                return JSONResponse({
                    "error": (
                        "this personal-homed secured set does not declare an "
                        "organization-keyed writeback slot"
                    ),
                }, status_code=403)
            # The token's stamped organization is the ONLY prefix source on a
            # cross-org writeback. The request supplies only the suffix.
            routed_key = schema_registry.derive_org_writeback_key(
                VAULT_SECURED_SET_ID, principal.org, key.strip(),
            )
        else:
            return JSONResponse(
                {"error": "a session or operator principal is required"},
                status_code=403,
            )
        with _store() as store:
            if class_id == "personal-root":
                root_pub = _personal_root_pub()
                candidates = [
                    record
                    for record in (store.get_class(i) for i in store.class_ids())
                    if record.governance
                    and record.governance.get("form") == "root-reachable"
                    and store.get_root_anchor(
                        record.governance["anchor_id"]
                    ).root_pub == root_pub
                ]
                if len(candidates) != 1:
                    raise VaultError(
                        "personal-root policy selector requires exactly one "
                        "current root-reachable class"
                    )
                class_id = candidates[0].class_id
            policy_class = store.get_class(class_id)
            if (
                not policy_class.governance
                or policy_class.governance.get("form") != "root-reachable"
            ):
                raise VaultError("the selected class is not personal-root reachable")
            anchor = store.get_root_anchor(policy_class.governance["anchor_id"])
            if anchor.root_pub != _personal_root_pub():
                raise VaultError("the selected class is not carried by the current personal root")
        setting_id = settings_ops.write_by_key(
            VAULT_SECURED_SET_ID,
            VAULT_CREDENTIAL_REVISION,
            routed_key,
            {"value": value},
            org=None,
            state="raw",
            vault_policy_class_id=class_id,
        )
        logger.info(
            "personal_vault_sealed caller_kind=%s caller=%s caller_org=%s "
            "key=%s policy_class=%s setting_id=%s",
            principal.kind.value,
            principal.subject,
            principal.org,
            routed_key,
            class_id,
            setting_id,
        )
        return JSONResponse({
            "id": setting_id,
            "set_id": VAULT_SECURED_SET_ID,
            "key": routed_key,
            "policy_class_id": class_id,
            "sealed": True,
        }, status_code=201)
    except SchemaValidationError as exc:
        return JSONResponse({"error": f"schema validation failed: {exc}"}, status_code=400)
    except settings_ops.VaultSealerMissing as exc:
        return JSONResponse({"error": str(exc)}, status_code=423)
    except (KeyError, TypeError, ValueError, VaultError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def remove_vault_credential(request: Request):
    """Remove one sealed credential from the caller's own namespace.

    The caller names the bare credential name; the store key is derived
    through the same routing seam ``vault_open`` and ``seal`` use, so remove
    accepts exactly the address read accepts. An explicit organization
    prefix is refused with the derives-the-namespace message rather than
    half-matching another namespace's row.
    """
    if (denied := api_auth.require_authenticated_api_caller(request)) is not None:
        return denied
    set_id = request.path_params["set_id"]
    name = request.path_params["name"]
    if set_id not in (VAULT_SECURED_SET_ID, VAULT_AUDITED_SET_ID):
        return JSONResponse(
            {"error": f"{set_id!r} is not a vault credential set"},
            status_code=400,
        )
    # Local import: vault_open_approvals pulls in the approval machinery,
    # which this module must not load at import time.
    from tools.dashboard.vault_open_approvals import _setting_route
    principal = api_auth.principal_from_request(request)
    try:
        routed_key, scope = _setting_route(principal, set_id, name)
        layers = settings_ops.layers_for(set_id, routed_key, org=scope)
        base = layers.get("base") or {}
        if not base.get("id"):
            return JSONResponse(
                {"error": (
                    f"no sealed credential named {name!r} in your vault "
                    "namespace"
                )},
                status_code=404,
            )
        if layers.get("shadowed_bases"):
            return JSONResponse(
                {"error": (
                    f"{name!r} resolves to more than one live row; removal "
                    "by name is ambiguous — name the setting id"
                )},
                status_code=409,
            )
        settings_ops.remove_setting(base["id"], org=scope)
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (ValueError, SchemaValidationError) as exc:
        # _setting_route names the operation it was written for; this is the
        # same derivation refusal, labelled for this verb.
        message = str(exc)
        if message.startswith("vault_open "):
            message = "vault_remove " + message[len("vault_open "):]
        return JSONResponse({"error": message}, status_code=400)
    logger.info(
        "vault_credential_removed caller_kind=%s caller=%s caller_org=%s "
        "set_id=%s key=%s setting_id=%s",
        principal.kind.value,
        principal.subject,
        principal.org,
        set_id,
        routed_key,
        base["id"],
    )
    return JSONResponse({"ok": True, "set_id": set_id, "key": routed_key})


async def enroll_password(request: Request):
    if (denied := _guard(request)) is not None:
        return denied
    try:
        body = await request.json()
        if "password" in body:
            raise VaultError("plaintext passwords are not accepted over HTTP; submit armor and public_key")
        with _store() as store:
            f = service.enroll_password_factor_material(
                store, body["factor_id"], body["public_key"], body["armor"]
            )
        return JSONResponse({"factor_id": f.factor_id, "public_key": f.public_key}, status_code=201)
    except (KeyError, TypeError, ValueError, VaultError, ArmorError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def _openers(body):
    raw = body.get("openers")
    if not isinstance(raw, dict) or not raw:
        raise VaultError("openers must be a non-empty factor_id -> seed_hex object")
    try:
        return {k: bytes.fromhex(v) for k, v in raw.items()}
    except (TypeError, ValueError) as exc:
        raise VaultError("openers contain invalid seed hex") from exc


async def create_class(request: Request):
    if (denied := _guard(request)) is not None:
        return denied
    try:
        body = await request.json()
        with _store() as store:
            cid = service.create_policy_class(store, body["policy"], body["factor_ids"], created_at=_now())
            record = store.get_class(cid)
        return JSONResponse(record.to_dict(), status_code=201)
    except (KeyError, TypeError, ValueError, VaultError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def class_factor(request: Request):
    if (denied := _guard(request)) is not None:
        return denied
    try:
        body = await request.json()
        with _store() as store:
            if "password" in body:
                raise VaultError("plaintext passwords are not accepted over HTTP; submit armor and public_key")
            factor = service.enroll_password_factor_material(
                store, body["factor_id"], body["public_key"], body["armor"]
            )
            record = store.get_class(request.path_params["class_id"])
            store.put_class(extend_class(record, _openers(body), factor))
            return JSONResponse(store.get_class(request.path_params["class_id"]).to_dict())
    except (KeyError, TypeError, ValueError, VaultError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def revoke(request: Request):
    if (denied := _guard(request)) is not None:
        return denied
    try:
        with _store() as store:
            service.revoke_and_rekey(store, request.path_params["class_id"], request.path_params["factor_id"], created_at=_now())
            return JSONResponse(store.get_class(request.path_params["class_id"]).to_dict())
    except VaultError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def show_class(request: Request):
    if (denied := _guard(request)) is not None:
        return denied
    try:
        with _store() as store:
            return JSONResponse(store.get_class(request.path_params["class_id"]).to_dict())
    except VaultError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


async def seal(request: Request):
    if (denied := _guard(request)) is not None:
        return denied
    try:
        body = await request.json()
        with _store() as store:
            service.seal_setting(
                store, request.path_params["name"], body["class_id"], body["genesis_id"],
                body["required_policy"],
            )
        # The CEK is an internal cryptographic intermediate, never a transport
        # result. This legacy low-level route records the wrap only; the real
        # Setting seal workflow is the operator-cookie ceremony.
        return JSONResponse({
            "setting_name": request.path_params["name"],
            "class_id": body["class_id"],
            "sealed": True,
        }, status_code=201)
    except (KeyError, TypeError, ValueError, VaultError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def open_setting(request: Request):
    if (denied := _guard(request)) is not None:
        return denied
    return JSONResponse({
        "error": (
            "content-encryption keys are never returned; read the secured "
            "Setting through a vault_open approval"
        ),
    }, status_code=410)


async def reseal(request: Request):
    if (denied := _guard(request)) is not None:
        return denied
    try:
        body = await request.json()
        with _store() as store:
            service.reseal_setting(store, request.path_params["name"], _openers(body))
        return JSONResponse({"setting_name": request.path_params["name"], "resealed": True})
    except (KeyError, TypeError, ValueError, VaultError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


ROUTES = [
    Route("/api/identity/factors", factors, methods=["GET"]),
    Route("/api/identity/vault-anchors", root_anchors, methods=["GET"]),
    Route("/api/identity/vault-anchors", enroll_root_anchor, methods=["POST"]),
    Route(
        "/api/identity/vault-anchors/{anchor_id}/classes",
        create_root_class,
        methods=["POST"],
    ),
    Route("/api/identity/vault-settings", seal_personal_setting, methods=["POST"]),
    Route(
        "/api/vault/credential/{set_id}/{name}",
        remove_vault_credential,
        methods=["DELETE"],
    ),
    Route("/api/identity/factors/password", enroll_password, methods=["POST"]),
    Route("/api/identity/classes", create_class, methods=["POST"]),
    Route("/api/identity/classes/{class_id}", show_class, methods=["GET"]),
    Route("/api/identity/classes/{class_id}/factors", class_factor, methods=["POST"]),
    Route("/api/identity/classes/{class_id}/factors/{factor_id}", revoke, methods=["DELETE"]),
    Route("/api/identity/settings/{name}/seal", seal, methods=["POST"]),
    Route("/api/identity/settings/{name}/open", open_setting, methods=["POST"]),
    Route("/api/identity/settings/{name}/reseal", reseal, methods=["POST"]),
]
