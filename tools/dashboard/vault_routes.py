"""Operator-authenticated HTTP surface for the password vault engine.

The route layer deliberately has no database-path argument.  It resolves the
personal database through the same declared-home resolver as settings and
content, then opens a short-lived :class:`VaultStore` for each request.
Passwords never cross this boundary; the browser submits canonical armor and
ephemeral opener seeds when an operation needs to open a class.
"""
from __future__ import annotations

from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard.api_auth import require_global_api_authority
from tools.vault import service
from tools.vault.errors import VaultError
from tools.vault.store import VaultStore
from tools.vault.key_holder import _scoped_db
from tools.vault.policy_class import extend_class
from tools.network.idkit.armor import ArmorError

_HOME_SET = "autonomy.identity.personal"


def _store():
    return VaultStore(_scoped_db(_HOME_SET, None))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guard(request):
    return require_global_api_authority(request)


async def factors(request: Request):
    if (denied := _guard(request)) is not None:
        return denied
    with _store() as store:
        return JSONResponse({
            "factors": [f.__dict__ for f in store.factors()],
            "classes": [store.get_class(i).to_dict() for i in store.class_ids()],
            "secrets": store.secret_names(),
        })


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
            cek = service.seal_setting(
                store, request.path_params["name"], body["class_id"], body["genesis_id"],
                body["required_policy"], _openers(body),
            )
        return JSONResponse({"setting_name": request.path_params["name"], "class_id": body["class_id"], "cek": cek.hex()}, status_code=201)
    except (KeyError, TypeError, ValueError, VaultError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def open_setting(request: Request):
    if (denied := _guard(request)) is not None:
        return denied
    try:
        body = await request.json()
        with _store() as store:
            cek = service.open_setting(store, request.path_params["name"], _openers(body))
        return JSONResponse({"setting_name": request.path_params["name"], "cek": cek.hex()})
    except (KeyError, TypeError, ValueError, VaultError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


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
    Route("/api/identity/factors/password", enroll_password, methods=["POST"]),
    Route("/api/identity/classes", create_class, methods=["POST"]),
    Route("/api/identity/classes/{class_id}", show_class, methods=["GET"]),
    Route("/api/identity/classes/{class_id}/factors", class_factor, methods=["POST"]),
    Route("/api/identity/classes/{class_id}/factors/{factor_id}", revoke, methods=["DELETE"]),
    Route("/api/identity/settings/{name}/seal", seal, methods=["POST"]),
    Route("/api/identity/settings/{name}/open", open_setting, methods=["POST"]),
    Route("/api/identity/settings/{name}/reseal", reseal, methods=["POST"]),
]
