"""Organization OIDC Settings projected into the existing gateway runtime."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets

from tools.dashboard import service_publication
from tools.graph import settings_ops
from tools.graph.schemas.service_auth import (
    SERVICE_AUTH_SET_ID, SERVICE_AUTH_SECRET_SET_ID, ServiceAuthV1,
)
from tools.graph.schemas.machine_vault import MACHINE_VAULT_AUDITED_SET_ID
from tools.network.service_auth import GateConfig, render_auth_config
from tools.network.storagekit.memory_cache import assert_memory_backed

RUNTIME_ROOT = Path("/run/autonomy-keycache/service-auth")


def configuration(org: str) -> dict:
    row = settings_ops.read_set_key(SERVICE_AUTH_SET_ID, "default", org=org, peers=[])
    return {**(row["payload"] if row else {"default_access": "public"}), "configured": row is not None}


def save_config(org: str, body: dict) -> dict:
    zones = [z for z in service_publication.list_zones(org) if z["state"] == "active"]
    if not zones:
        raise service_publication.ServicePublicationError("domain_required", 400)
    payload = {key: body.get(key) for key in ("provider", "issuer", "client_id", "default_access")}
    ServiceAuthV1.validate(payload)
    secret = body.get("client_secret")
    if secret:
        settings_ops.write_by_key(SERVICE_AUTH_SECRET_SET_ID, 1, "default", {"client_secret": secret}, org=org)
    elif not configuration(org)["configured"]:
        raise service_publication.ServicePublicationError("client_secret_required", 400)
    settings_ops.upsert_by_key(SERVICE_AUTH_SET_ID, 1, "default", payload, org=org)
    return configuration(org)


def cookie_secret(org: str) -> bytes:
    key = f"service-auth-cookie:{org}"
    row = settings_ops.read_set_key(MACHINE_VAULT_AUDITED_SET_ID, key, org="machine", peers=[])
    if row is None:
        value = secrets.token_hex(32)
        settings_ops.write_by_key(MACHINE_VAULT_AUDITED_SET_ID, 1, key, {"value": value}, org="machine")
    else:
        value = row["payload"]["value"]
    return bytes.fromhex(value)


def materialize_helper(org: str, hostname: str, port: int):
    from tools.dashboard.web_gateway_supervisor import AuthHelper

    config = configuration(org)
    row = settings_ops.read_set_key(SERVICE_AUTH_SECRET_SET_ID, "default", org=org, peers=[])
    gate = GateConfig("org-oidc", (hostname,), config["issuer"], config["client_id"], port)
    files = {
        "oauth2-proxy.cfg": render_auth_config(gate, listener_port=port).encode(),
        "client-secret": row["payload"]["client_secret"].encode(),
        "cookie-secret": cookie_secret(org),
    }
    assert_memory_backed(RUNTIME_ROOT.parent)
    directory = RUNTIME_ROOT / f"org-oidc-{org}"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256()
    for name, content in files.items():
        path = directory / name
        digest.update(name.encode() + b"\0" + content)
        if not path.exists() or path.read_bytes() != content:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
    return AuthHelper(f"org-oidc:{org}", str(directory), digest.hexdigest())
