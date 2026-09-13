"""Narrow connector -> warm dashboard key custody (graph://6b2ede9a-854).

TCP authenticates bearer possession, NOT peer PID. Recorded PID/start identity
only bounds the credential's lifetime. No vault delegate crosses this seam.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import socketserver
import threading

from tools.graph import settings_ops

SET_ID = "autonomy.machine.serving-connector"
PROTOCOL_VERSION = 1
MAX_MESSAGE = 16384
TIMEOUT = 5
_listener = None
_listener_lock = threading.Lock()


def process_start(pid: int) -> str | None:
    """Kernel birth identity, including boot ID; a reused PID is not a holder."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + stat[19]
    except (OSError, IndexError, ValueError):
        return None


def record(credential_id: str):
    row = settings_ops.read_set_key(SET_ID, credential_id, org="machine")
    return row if row and isinstance(row.get("payload"), dict) else None


def _write(credential_id, payload):
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(SET_ID, 1, credential_id, payload, org="machine")


def _live(payload):
    return (payload.get("protocol_version") == PROTOCOL_VERSION
            and isinstance(payload.get("pid"), int)
            and payload.get("process_start") is not None
            and process_start(payload["pid"]) == payload["process_start"])


def _authorized_grant(request):
    """Authenticate the connector request and return its owned live grant."""
    from tools.dashboard.link_serving import check_grant

    credential_id = request.get("credential_id")
    auth = request.get("auth")
    if (not isinstance(credential_id, str) or not isinstance(auth, str)
            or request.get("protocol_version") != PROTOCOL_VERSION):
        raise PermissionError("connector authentication refused")
    row = record(credential_id)
    payload = row["payload"] if row else {}
    digest = hashlib.sha256(auth.encode()).hexdigest()
    if (not hmac.compare_digest(digest, payload.get("token_hash", ""))
            or payload.get("org_uuid") != credential_id or not _live(payload)):
        raise PermissionError("connector authentication refused")
    token = request.get("token")
    org = payload["organization"] or None
    grant = check_grant(token, org=org)
    if not grant:
        raise PermissionError("link unavailable")
    return token, org, grant


def resolve(request):
    """Resolve one mandatory public-link key for an authenticated connector."""
    from tools.dashboard.link_channel_key import CHANNEL_KEY_TARGET_TYPES, channel_key_for
    from tools.dashboard.link_serving import check_grant

    token, org, grant = _authorized_grant(request)
    if grant["target_type"] not in CHANNEL_KEY_TARGET_TYPES:
        raise PermissionError("link unavailable")
    if not grant.get("channel_pub"):
        raise PermissionError("link has no channel key")
    key = channel_key_for(token, org)
    if key.public_hex != grant["channel_pub"] or check_grant(token, org=org) != grant:
        raise PermissionError("link unavailable")
    return {"ok": True, "seed": key.private_hex}


def resolve_channel(request):
    """Classify Fleet enrollment explicitly; all other channels require keys."""
    token, org, grant = _authorized_grant(request)
    if grant["target_type"] == "fleet:join":
        return {"ok": True, "protocol": "fleet-enrollment"}
    keyed = resolve(request)
    return {**keyed, "protocol": "public-link"}


def _read_message(stream):
    line = stream.readline(MAX_MESSAGE + 1)
    if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
        raise ValueError("invalid resolver message")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("invalid resolver message")
    return value


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(TIMEOUT)
        try:
            response = resolve_channel(_read_message(self.rfile))
        except Exception:
            # Never echo requests, credentials or vault errors to the socket/log.
            response = {"ok": False, "error": "link key resolution refused"}
        self.wfile.write(json.dumps(response).encode() + b"\n")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True


def listener_port():
    global _listener
    with _listener_lock:
        if _listener is None:
            _listener = _Server(("127.0.0.1", 0), _Handler)
            threading.Thread(target=_listener.serve_forever, daemon=True,
                             name="connector-key-resolution").start()
        return _listener.server_address[1]


def register(pid, credential_id, org, boot_commit):
    """Persist hash only; caller sends returned bootstrap down the child pipe."""
    start = process_start(pid)
    if start is None:
        raise RuntimeError("connector exited before credential handoff")
    auth = secrets.token_hex(32)
    _write(credential_id, {
        "token_hash": hashlib.sha256(auth.encode()).hexdigest(),
        "organization": org or "", "org_uuid": credential_id,
        "pid": pid, "process_start": start, "boot_commit": boot_commit or "unknown",
        "protocol_version": PROTOCOL_VERSION, "resolver_port": listener_port(),
    })
    return {"credential_id": credential_id, "auth": auth, "protocol_version": PROTOCOL_VERSION}


def adopt(credential_id, org, pid):
    row = record(credential_id)
    payload = dict(row["payload"]) if row else {}
    if payload.get("pid") != pid or payload.get("organization") != (org or "") or not _live(payload):
        return False
    payload["resolver_port"] = listener_port()
    _write(credential_id, payload)
    return True


def retire(pid):
    """Remove only this process's record, never a replacement's credential."""
    with settings_ops.identity_write_context():
        for member in settings_ops.read_owned_set(SET_ID, org="machine").members:
            if member.payload.get("pid") == pid:
                settings_ops.remove_setting(member.id, org="machine")


def read_bootstrap(fd):
    with os.fdopen(fd, "rb") as stream:
        return _read_message(stream)


def client(bootstrap):
    """Resolve an explicit channel protocol fresh per OPEN, without a cache."""
    def fetch(token):
        from tools.network.idkit import KeyPair
        row = record(bootstrap["credential_id"])
        if row is None:
            raise PermissionError("connector credential unavailable")
        port = row["payload"]["resolver_port"]
        request = {**bootstrap, "token": token}
        with socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT) as sock:
            sock.sendall(json.dumps(request).encode() + b"\n")
            with sock.makefile("rb") as stream:
                response = _read_message(stream)
        if response.get("ok") is not True:
            raise PermissionError("link key resolution refused")
        if response.get("protocol") == "fleet-enrollment":
            return {"protocol": "fleet-enrollment"}
        if response.get("protocol") != "public-link":
            raise PermissionError("link key resolution refused")
        return {
            "protocol": "public-link",
            "key": KeyPair.from_private_hex(response["seed"]),
        }

    async def fetch_async(token):
        return await asyncio.to_thread(fetch, token)
    return fetch_async
