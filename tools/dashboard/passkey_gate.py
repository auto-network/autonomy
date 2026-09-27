"""The dashboard's side of its passkey gate (graph://c9d72ea4-feb, option O2d).

The gate helper (tools/network/passkey_gate.py) runs beside the Service
gateway and checks passkeys; this module owns the state it checks against:

* the gate record, ``autonomy.dashboard.passkey-gate`` (personal-homed):
  enrolled gate passkeys and the enrollment state;
* the one-time enrollment token: its sha256 in the record, its plaintext in
  the machine vault while enrollment is open (the onboarding reach step and
  the Remote access screen show the link until it is used or expires);
* the helper's runtime directory under the memory-backed keycache: the
  record's projection (``gate.json``), the cookie key and the helper
  secret, rewritten whenever the record changes, read by the helper on
  every request (no restart);
* the two callbacks the helper makes over the dashboard's plain listener,
  authenticated with the helper secret: a verified registration (which
  closes enrollment) and a new sign count.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import time
from pathlib import Path

from tools.dashboard.service_auth import RUNTIME_ROOT
from tools.graph import settings_ops
from tools.graph.schemas.dashboard_passkey_gate import (
    PASSKEY_GATE_KEY, PASSKEY_GATE_REVISION, PASSKEY_GATE_SET_ID,
)
from tools.graph.schemas.machine_vault import MACHINE_VAULT_AUDITED_SET_ID
from tools.network import passkey_gate as helper
from tools.network.storagekit.memory_cache import assert_memory_backed

logger = logging.getLogger(__name__)

HELPER_ID = helper.HELPER_ID
#: A one-time enrollment token is good for ten minutes (graph://c9d72ea4-feb Q2).
ENROLLMENT_TTL_S = 600
COOKIE_VAULT_KEY = "dashboard-passkey-cookie"
HELPER_VAULT_KEY = "dashboard-passkey-helper"
TOKEN_VAULT_KEY = "dashboard-passkey-enrollment"


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ── the record ────────────────────────────────────────────────────────────

def record() -> dict:
    """The gate record, or its empty shape before any enrollment."""
    row = settings_ops.read_set_key(PASSKEY_GATE_SET_ID, PASSKEY_GATE_KEY, org=None, peers=[])
    if row is None:
        return {"credentials": [], "enrollment": None, "updated_at": _utc_now()}
    payload = dict(row["payload"])
    payload.setdefault("credentials", [])
    payload.setdefault("enrollment", None)
    return payload


def _save(payload: dict) -> dict:
    payload = dict(payload)
    payload["updated_at"] = _utc_now()
    if payload.get("enrollment") is None:
        payload.pop("enrollment", None)
    settings_ops.upsert_by_key(PASSKEY_GATE_SET_ID, PASSKEY_GATE_REVISION, PASSKEY_GATE_KEY, payload, org=None)
    return record()


def enrolled_count() -> int:
    return len(record().get("credentials") or [])


def enrollment_state(now: float | None = None) -> dict:
    """``{open, expires_at}`` as the status routes report it."""
    enrollment = record().get("enrollment") or {}
    now = time.time() if now is None else now
    is_open = bool(enrollment.get("open")) and float(enrollment.get("expires_at") or 0) > now
    return {"open": is_open, "expires_at": enrollment.get("expires_at") if is_open else None}


# ── enrollment token ──────────────────────────────────────────────────────

def open_enrollment(*, opened_by: str, now: float | None = None) -> dict:
    """Mint a fresh one-time token, record its hash and expiry, keep its
    plaintext in the machine vault while open. Returns ``{token, expires_at}``.
    Re-opening replaces any earlier token."""
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(32)
    expires_at = int(now + ENROLLMENT_TTL_S)
    payload = record()
    payload["enrollment"] = {
        "open": True, "token_sha256": helper.token_sha256(token),
        "expires_at": expires_at, "opened_by": opened_by,
    }
    _save(payload)
    settings_ops.write_by_key(MACHINE_VAULT_AUDITED_SET_ID, 1, TOKEN_VAULT_KEY,
                              {"value": json.dumps({"token": token, "expires_at": expires_at})}, org="machine")
    _rematerialize()
    return {"token": token, "expires_at": expires_at}


def close_enrollment() -> None:
    payload = record()
    if payload.get("enrollment"):
        payload["enrollment"] = None
        _save(payload)
    _clear_token()
    _rematerialize()


def open_token(now: float | None = None) -> dict | None:
    """The plaintext token while enrollment is open, else None (the vault
    row is read only here; a cold vault reads as no token)."""
    state = enrollment_state(now)
    if not state["open"]:
        return None
    try:
        row = settings_ops.read_set_key(MACHINE_VAULT_AUDITED_SET_ID, TOKEN_VAULT_KEY, org="machine", peers=[])
        value = json.loads(row["payload"]["value"]) if row else None
    except Exception:
        return None
    if not isinstance(value, dict) or not value.get("token"):
        return None
    if helper.token_sha256(value["token"]) != (record().get("enrollment") or {}).get("token_sha256"):
        return None
    return {"token": value["token"], "expires_at": state["expires_at"]}


def enrollment_url(origin: str | None, now: float | None = None) -> str | None:
    token = open_token(now)
    if not token or not origin:
        return None
    return f"{origin}/oauth2/enroll?token={token['token']}"


def _clear_token() -> None:
    try:
        settings_ops.write_by_key(MACHINE_VAULT_AUDITED_SET_ID, 1, TOKEN_VAULT_KEY, {"value": "{}"}, org="machine")
    except Exception:
        logger.warning("could not clear the enrollment token from the machine vault", exc_info=True)


# ── the helper's callbacks ────────────────────────────────────────────────

class GateRefusal(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def register_credential(body: dict, *, now: float | None = None) -> dict:
    """The helper verified a registration under the open token: record the
    credential and close enrollment. One passkey per token, by construction."""
    payload = record()
    token = body.get("token") if isinstance(body, dict) else None
    if not helper.enrollment_open(payload, token, now=now):
        raise GateRefusal("enrollment_closed", 403)
    try:
        credential = {
            "credential_id": str(body["credential_id"]),
            "public_key": str(body["public_key"]),
            "sign_count": int(body["sign_count"]),
            "transports": [t for t in (body.get("transports") or []) if isinstance(t, str)],
            "rp_id": str(body["rp_id"]),
            "created_at": _utc_now(),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise GateRefusal("invalid_credential", 400) from exc
    if any(row.get("credential_id") == credential["credential_id"] for row in payload["credentials"]):
        raise GateRefusal("credential_exists", 409)
    payload["credentials"] = [*payload["credentials"], credential]
    payload["enrollment"] = None
    saved = _save(payload)
    _clear_token()
    _rematerialize()
    return saved


def update_sign_count(credential_id: str, sign_count: int) -> None:
    payload = record()
    for row in payload["credentials"]:
        if row.get("credential_id") == credential_id and int(sign_count) > int(row.get("sign_count") or 0):
            row["sign_count"] = int(sign_count)
            _save(payload)
            _rematerialize()
            return


def revoke_credential(credential_id: str) -> dict:
    payload = record()
    kept = [row for row in payload["credentials"] if row.get("credential_id") != credential_id]
    if len(kept) == len(payload["credentials"]):
        raise GateRefusal("unknown_credential", 404)
    payload["credentials"] = kept
    saved = _save(payload)
    _rematerialize()
    return saved


def helper_authorized(headers) -> bool:
    """The helper's callbacks carry ``Authorization: Bearer <helper secret>``."""
    value = headers.get("authorization") or ""
    if not value.startswith("Bearer "):
        return False
    try:
        return hmac.compare_digest(value[len("Bearer "):].strip(), helper_secret())
    except Exception:
        return False


# ── secrets ───────────────────────────────────────────────────────────────

def _machine_secret(key: str) -> str:
    row = settings_ops.read_set_key(MACHINE_VAULT_AUDITED_SET_ID, key, org="machine", peers=[])
    if row is None:
        value = secrets.token_hex(32)
        settings_ops.write_by_key(MACHINE_VAULT_AUDITED_SET_ID, 1, key, {"value": value}, org="machine")
        return value
    return row["payload"]["value"]


def cookie_secret() -> bytes:
    return bytes.fromhex(_machine_secret(COOKIE_VAULT_KEY))


def helper_secret() -> str:
    return _machine_secret(HELPER_VAULT_KEY)


# ── the helper's runtime directory ────────────────────────────────────────

def runtime_dir() -> Path:
    return RUNTIME_ROOT / HELPER_ID


def projection(hostname: str, dashboard_upstream: str) -> dict:
    """What the helper reads: public credential data and the enrollment
    hash, never the token, never the vault."""
    payload = record()
    enrollment = payload.get("enrollment") or None
    return {
        "rp_id": hostname,
        "origin": f"https://{hostname}",
        "dashboard_upstream": dashboard_upstream,
        "credentials": [
            {"credential_id": row["credential_id"], "public_key": row["public_key"],
             "sign_count": int(row.get("sign_count") or 0), "transports": row.get("transports") or []}
            for row in payload.get("credentials") or [] if row.get("rp_id") == hostname
        ],
        "enrollment": (
            {"open": True, "token_sha256": enrollment["token_sha256"], "expires_at": enrollment["expires_at"]}
            if enrollment and enrollment.get("open") else None
        ),
    }


def materialize_helper(hostname: str, port: int, dashboard_upstream: str):
    """Write the helper's runtime files and describe its container. Called by
    the gateway planner for the dashboard's personal route; the returned
    revision changes whenever the projection does, which re-renders the
    route (the helper itself re-reads the files per request)."""
    from tools.dashboard.web_gateway_supervisor import AuthHelper

    files = {
        "gate.json": json.dumps(projection(hostname, dashboard_upstream), sort_keys=True).encode(),
        "cookie-secret": cookie_secret().hex().encode(),
        "helper-secret": helper_secret().encode(),
    }
    assert_memory_backed(RUNTIME_ROOT.parent)
    directory = runtime_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256()
    for name, content in files.items():
        path = directory / name
        digest.update(name.encode() + b"\0" + content)
        if not path.exists() or path.read_bytes() != content:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
    image, app_mount = own_runtime()
    revision = digest.hexdigest()
    service = helper.render_helper_service(directory, revision, image=image, port=port, app_mount=app_mount)
    _remember(hostname, port, dashboard_upstream)
    return AuthHelper(HELPER_ID, str(directory), revision, service=service)


_last_materialization: dict | None = None


def _remember(hostname: str, port: int, dashboard_upstream: str) -> None:
    global _last_materialization
    _last_materialization = {"hostname": hostname, "port": port, "dashboard_upstream": dashboard_upstream}


def _rematerialize() -> None:
    """Rewrite the helper's projection after a record change and ask the
    gateway to re-plan (best effort; the next plan rewrites it anyway)."""
    if _last_materialization is None:
        return
    try:
        materialize_helper(**_last_materialization)
    except Exception:
        logger.warning("could not rewrite the passkey gate projection", exc_info=True)
        return
    try:
        import asyncio

        from tools.dashboard import web_gateway_supervisor

        loop = asyncio.get_running_loop()
        loop.create_task(web_gateway_supervisor.request_reload())
    except Exception:
        pass


_own_runtime_cache: tuple[str, dict | None] | None = None


def own_runtime() -> tuple[str, dict | None]:
    """The dashboard container's image reference and its ``/app`` mount, so the
    helper runs exactly the code this dashboard runs (a release's pinned image
    on a published node; the bind-mounted checkout in the compose simulation).
    Read once per process; a host-process dashboard has neither and cannot
    publish itself as a Service anyway."""
    global _own_runtime_cache
    if _own_runtime_cache is not None:
        return _own_runtime_cache
    from tools.dashboard.service_publication import _own_dashboard_container_id

    container = _own_dashboard_container_id()
    if not container:
        raise RuntimeError("the dashboard is not running in a container")
    result = subprocess.run(["docker", "inspect", container], capture_output=True, text=True, timeout=5, check=False)
    documents = json.loads(result.stdout) if result.returncode == 0 else []
    if not isinstance(documents, list) or len(documents) != 1:
        raise RuntimeError("could not inspect the dashboard container")
    document = documents[0]
    image = (document.get("Config") or {}).get("Image") or document.get("Image")
    if not isinstance(image, str) or not image:
        raise RuntimeError("the dashboard container names no image")
    app_mount = None
    for mount in document.get("Mounts") or []:
        if not isinstance(mount, dict) or mount.get("Destination") != "/app":
            continue
        if mount.get("Type") == "volume" and mount.get("Name"):
            app_mount = {"type": "volume", "source": mount["Name"]}
        elif mount.get("Type") == "bind" and mount.get("Source"):
            app_mount = {"type": "bind", "source": mount["Source"]}
        break
    _own_runtime_cache = (image, app_mount)
    return _own_runtime_cache
