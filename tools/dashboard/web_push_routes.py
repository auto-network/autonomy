"""Headless browser API for Web Push installations and preferences.

The browser supplies locators and subscription material, never authority.
Cookie-authenticated calls resolve the durable personal root at the server;
background refresh uses a narrow, rotating credential for one device only.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import api_auth
from tools.dashboard.attention_registry import build_production_attention_registry
from tools.dashboard.dao import web_push as web_push_dao
from tools.dashboard.identity_routes import (
    StablePersonalIdentityUnavailable,
    resolve_stable_personal_root_public_key,
)


DB_PATH = web_push_dao.DB_PATH
KEY_DIR = web_push_dao.KEY_DIR
LEGACY_KEY_PATH = web_push_dao.LEGACY_KEY_PATH
MAX_BODY_BYTES = 12 * 1024
PAYLOAD_LIMIT = 2048
_OWNER_DOMAIN = b"autonomy:web-push-owner:v1\0"


def _store() -> web_push_dao.WebPushStore:
    return web_push_dao.WebPushStore(DB_PATH)


def _custody(store: web_push_dao.WebPushStore) -> web_push_dao.VapidKeyCustody:
    return web_push_dao.VapidKeyCustody(
        store, key_dir=KEY_DIR, legacy_key_path=LEGACY_KEY_PATH,
    )


def stable_operator_subject() -> str:
    root = resolve_stable_personal_root_public_key()
    return hashlib.sha256(_OWNER_DOMAIN + bytes.fromhex(root)).hexdigest()


def _operator_cookie_only(request: Request) -> JSONResponse | None:
    principal = api_auth.principal_from_request(request)
    if principal.kind is api_auth.ApiPrincipalKind.OPERATOR_COOKIE:
        return None
    if principal.kind is api_auth.ApiPrincipalKind.COMPATIBILITY:
        return JSONResponse(
            {"ok": False, "error": "authentication_required"}, status_code=401,
        )
    return JSONResponse(
        {"ok": False, "error": "operator_browser_authority_required"},
        status_code=403,
    )


def _same_origin(request: Request) -> str:
    supplied_raw = request.headers.get("origin")
    if not supplied_raw:
        raise ValueError("same-origin Origin header required")
    supplied = urlsplit(supplied_raw)
    expected = urlsplit(str(request.base_url))
    try:
        supplied_port = supplied.port
        expected_port = expected.port
    except ValueError as exc:
        raise ValueError("same-origin HTTPS request required") from exc
    if (
        supplied.scheme != "https"
        or expected.scheme != "https"
        or supplied.hostname is None
        or expected.hostname is None
        or supplied.hostname.rstrip(".").lower()
        != expected.hostname.rstrip(".").lower()
        or supplied_port != expected_port
        or supplied.username
        or supplied.password
        or supplied.path not in ("", "/")
        or supplied.query
        or supplied.fragment
    ):
        raise ValueError("same-origin HTTPS request required")
    host = supplied.hostname.rstrip(".").lower()
    return f"https://{host}" + (
        f":{supplied_port}" if supplied_port not in (None, 443) else ""
    )


async def _json_body(request: Request) -> dict:
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type.strip().lower() != "application/json":
        raise TypeError("application/json required")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise OverflowError("request is too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("body must be a JSON object")
    return value


def _applications() -> tuple[dict, ...]:
    registry = build_production_attention_registry()
    return tuple({
        "application": item.application_scope,
        "label": item.label,
        "enabled": item.enabled,
    } for item in registry.applications)


def _registered_application(name: str) -> bool:
    return any(item["application"] == name for item in _applications())


def _subscription(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "endpoint", "expiration_time", "keys", "vapid_key_id",
    }:
        raise ValueError(
            "subscription must contain endpoint, expiration_time, keys, and vapid_key_id"
        )
    # Keep endpoint and key validation shared with the sender's egress policy.
    from tools.dashboard import web_push

    validated = web_push._validate_subscription({
        "endpoint": value["endpoint"],
        "expirationTime": value["expiration_time"],
        "keys": value["keys"],
    })
    parsed = urlsplit(validated["endpoint"])
    endpoint_origin = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port not in (None, 443):
        endpoint_origin += f":{parsed.port}"
    key_id = value["vapid_key_id"]
    if not isinstance(key_id, str) or not web_push_dao.KEY_ID_RE.fullmatch(key_id):
        raise ValueError("vapid_key_id is invalid")
    return {
        "endpoint": validated["endpoint"],
        "endpoint_hash": hashlib.sha256(validated["endpoint"].encode()).hexdigest(),
        "endpoint_origin": endpoint_origin,
        "p256dh": validated["keys"]["p256dh"],
        "auth_secret": validated["keys"]["auth"],
        "vapid_key_id": key_id,
        "expiration_time": validated["expiration_time"],
    }


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, StablePersonalIdentityUnavailable):
        return JSONResponse(
            {"ok": False, "error": "personal_identity_unavailable"}, status_code=409,
        )
    if isinstance(exc, web_push_dao.WebPushStoreError):
        if exc.code == "device_not_found":
            status = 404
        elif exc.code in {
            "device_conflict", "endpoint_conflict", "subscription_conflict",
            "vapid_key_mismatch", "vapid_rotation_conflict",
        }:
            status = 409
        elif exc.code in {
            "web_push_runtime_unavailable", "vapid_key_unavailable",
            "vapid_key_material_mismatch", "unsupported_schema_version",
            "unsupported_subscription_schema", "ambiguous_active_vapid_key",
            "vapid_key_directory_unsafe", "vapid_key_directory_owner_mismatch",
            "vapid_key_directory_mode_mismatch", "vapid_key_file_unsafe",
            "vapid_key_file_owner_mismatch", "vapid_key_file_mode_mismatch",
            "vapid_key_file_invalid", "vapid_key_metadata_invalid",
            "vapid_key_path_invalid", "legacy_vapid_key_unsafe",
            "legacy_vapid_key_owner_mismatch", "legacy_vapid_key_invalid",
        }:
            status = 503
        else:
            status = 422
        return JSONResponse({"ok": False, "error": exc.code}, status_code=status)
    if isinstance(exc, TypeError):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=415)
    if isinstance(exc, OverflowError):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=413)
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)


async def api_config(request: Request) -> JSONResponse:
    refused = _operator_cookie_only(request)
    if refused is not None:
        return refused
    try:
        operator_subject = stable_operator_subject()
        store = _store()
        active = _custody(store).ensure_active()
        applications = _applications()
        preferences = store.preferences(
            operator_subject, (item["application"] for item in applications),
        )
        devices = store.list_devices(operator_subject)
    except Exception as exc:
        return _error(exc)
    return JSONResponse({
        "ok": True,
        "ready": True,
        "vapid": {"key_id": active.key_id, "public_key": active.public_key},
        # Compatibility fields consumed by the already-shipped controller.
        "vapid_key_id": active.key_id,
        "application_server_key": active.public_key,
        "payload_limit": PAYLOAD_LIMIT,
        "prerequisites": {
            "secure_context": True,
            "service_worker": True,
            "push_manager": True,
            "notifications": True,
            "ios_home_screen_required": True,
        },
        "applications": applications,
        "preferences": preferences,
        "devices": devices,
    })


async def api_put_device(request: Request) -> JSONResponse:
    refused = _operator_cookie_only(request)
    if refused is not None:
        return refused
    try:
        vapid_subject = _same_origin(request)
        body = await _json_body(request)
        allowed = {
            "subscription", "device_label", "platform_family", "browser_family",
            "max_detail",
        }
        if set(body) - allowed or "subscription" not in body:
            raise ValueError("device body contains unsupported fields")
        operator_subject = stable_operator_subject()
        subscription = _subscription(body["subscription"])
        result = _store().enroll(
            operator_subject=operator_subject,
            device_id=request.path_params["device_id"],
            max_detail=body.get("max_detail", "generic"),
            device_label=body.get("device_label"),
            platform_family=body.get("platform_family"),
            browser_family=body.get("browser_family"),
            vapid_subject=vapid_subject,
            **subscription,
        )
    except Exception as exc:
        return _error(exc)
    return JSONResponse({
        "ok": True,
        "device_id": result.device_id,
        "status": result.status,
        "device_update_token": result.device_update_token,
        "token_version": result.token_version,
    })


async def api_devices(request: Request) -> JSONResponse:
    refused = _operator_cookie_only(request)
    if refused is not None:
        return refused
    try:
        devices = _store().list_devices(stable_operator_subject())
    except Exception as exc:
        return _error(exc)
    return JSONResponse({"ok": True, "devices": devices})


async def api_preference(request: Request) -> JSONResponse:
    refused = _operator_cookie_only(request)
    if refused is not None:
        return refused
    try:
        _same_origin(request)
        application = request.path_params["application"]
        if not _registered_application(application):
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        body = await _json_body(request)
        if set(body) != {"mode"}:
            raise ValueError("preference body must contain only mode")
        mode = _store().set_preference(
            stable_operator_subject(), application, body["mode"],
        )
    except Exception as exc:
        return _error(exc)
    return JSONResponse({"ok": True, "application": application, "mode": mode})


async def api_delete_device(request: Request) -> JSONResponse:
    refused = _operator_cookie_only(request)
    if refused is not None:
        return refused
    try:
        _same_origin(request)
        retired = _store().retire(
            stable_operator_subject(), request.path_params["device_id"],
            reason="operator_retired",
        )
    except Exception as exc:
        return _error(exc)
    if not retired:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    from tools.dashboard import web_push
    web_push.wake_worker()
    return JSONResponse({"ok": True, "retired": True})


def _device_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if (
        separator != " "
        or scheme != "WebPushDevice"
        or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token)
    ):
        raise web_push_dao.WebPushStoreError("device_not_found")
    return token


async def api_refresh_device(request: Request) -> JSONResponse:
    try:
        token = _device_token(request)
        body = await _json_body(request)
        if set(body) != {"old_endpoint_hash", "token_version", "subscription"}:
            raise ValueError(
                "refresh body must contain old_endpoint_hash, token_version, and subscription"
            )
        subscription = (
            None if body["subscription"] is None else _subscription(body["subscription"])
        )
        result = _store().refresh(
            device_id=request.path_params["device_id"],
            device_update_token=token,
            token_version=body["token_version"],
            old_endpoint_hash=body["old_endpoint_hash"],
            subscription=subscription,
        )
    except Exception as exc:
        return _error(exc)
    if result is None:
        from tools.dashboard import web_push
        web_push.wake_worker()
        return JSONResponse({"ok": True, "retired": True})
    return JSONResponse({
        "ok": True,
        "device_id": result.device_id,
        "status": result.status,
        "device_update_token": result.device_update_token,
        "token_version": result.token_version,
    })


ROUTES = [
    Route("/api/web-push/config", api_config, methods=["GET"]),
    Route("/api/web-push/devices", api_devices, methods=["GET"]),
    Route("/api/web-push/devices/{device_id}", api_put_device, methods=["PUT"]),
    Route("/api/web-push/devices/{device_id}", api_delete_device, methods=["DELETE"]),
    Route(
        "/api/web-push/devices/{device_id}/refresh",
        api_refresh_device,
        methods=["POST"],
    ),
    Route(
        "/api/web-push/preferences/{application}",
        api_preference,
        methods=["PATCH"],
    ),
]


__all__ = ["ROUTES", "stable_operator_subject"]
