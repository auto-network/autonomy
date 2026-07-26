"""Cross-language and live D21 proofs for the Node org ceremonies."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.network.idkit import KeyPair
from tools.network.idkit.armor import decrypt_root_key, parse_armor
from tools.network.idkit.keys import verify_signature
from tools.network.registry.signing import request_signing_input

REPO = Path(__file__).resolve().parents[3]
NODE_TEST = (
    REPO / "tools" / "dashboard" / "static" / "js"
    / "ceremony" / "tests" / "organization.test.mjs"
)
COMMAND = (
    REPO / "tools" / "dashboard" / "static" / "js"
    / "ceremony" / "node" / "org-commands.mjs"
)
ORG_PASSPHRASE = "node org identity armor passphrase"


def _node_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key != "AUTONOMY_ORG_PASSPHRASE"
    }


def _run_with_passphrase(arguments: list[str], passphrase: str) -> subprocess.CompletedProcess:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, f"{passphrase}\n".encode())
    finally:
        os.close(write_fd)
    try:
        return subprocess.run(
            [
                "node",
                str(COMMAND),
                *arguments,
                "--passphrase-fd",
                str(read_fd),
            ],
            cwd=REPO,
            env=_node_environment(),
            pass_fds=(read_fd,),
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        os.close(read_fd)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _live_server(app, port: int):
    server = uvicorn.Server(uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def _d21_registry(posts: list[dict]) -> Starlette:
    bindings: dict[str, dict] = {}

    async def register(request: Request):
        envelope = await request.json()
        posts.append(envelope)
        payload = envelope.get("payload")
        if (
            set(envelope) != {"v", "signer", "ts", "payload", "sig"}
            or envelope.get("v") != 1
            or not isinstance(payload, dict)
            or "org_uuid" in payload
            or envelope.get("signer") != payload.get("root_pub")
        ):
            return JSONResponse(
                {"detail": "D21 registration must be root-direct"},
                status_code=403,
            )
        allowed = {"root_pub", "recovery_policy", "recovery_pub"}
        if not set(payload).issubset(allowed):
            return JSONResponse(
                {"detail": "unknown registration field"},
                status_code=400,
            )
        try:
            verify_signature(
                envelope["signer"],
                envelope["sig"],
                request_signing_input(
                    "POST",
                    "/v1/orgs",
                    envelope["ts"],
                    envelope["signer"],
                    payload,
                ),
            )
        except Exception as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)

        existing = bindings.get(payload["root_pub"])
        if existing:
            return JSONResponse(existing, status_code=200)
        binding = {
            "org_uuid": str(uuid.uuid4()),
            "root_pub": payload["root_pub"],
            "expires_at": int(time.time()) + 30 * 86400,
        }
        bindings[payload["root_pub"]] = binding
        return JSONResponse(binding, status_code=201)

    return Starlette(routes=[
        Route("/v1/orgs", register, methods=["POST"]),
    ])


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_organization_core_matches_python_idkit():
    result = subprocess.run(
        ["node", str(NODE_TEST)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    vector = json.loads(result.stdout)

    parsed = parse_armor(vector["armor"])
    assert parsed["root_pub"] == vector["rootPub"]
    opened = decrypt_root_key(vector["armor"], vector["passphrase"])
    assert opened.private_hex == vector["seedHex"]
    assert opened.public_hex == vector["rootPub"]

    envelope = vector["envelope"]
    assert set(envelope) == {"v", "signer", "ts", "payload", "sig"}
    assert set(envelope["payload"]) == {"root_pub", "recovery_policy"}
    assert "org_uuid" not in envelope["payload"]
    verify_signature(
        envelope["signer"],
        envelope["sig"],
        request_signing_input(
            "POST",
            "/v1/orgs",
            envelope["ts"],
            envelope["signer"],
            envelope["payload"],
        ),
    )
    assert KeyPair.from_private_hex(
        vector["recoverySeedHex"]
    ).public_hex == vector["recoveryPub"]
    assert vector["recoverySeedHex"] in vector["recoveryBlock"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_commands_write_armor_and_register_on_live_d21_contract(tmp_path):
    armor_path = tmp_path / "org-root.armor"
    created = _run_with_passphrase(
        [
            "create-org-identity",
            "--armor-output",
            str(armor_path),
        ],
        ORG_PASSPHRASE,
    )
    assert created.returncode == 0, created.stdout + "\n" + created.stderr
    identity = json.loads(created.stdout)
    assert identity["armor_path"] == str(armor_path)
    armor = armor_path.read_text()
    assert decrypt_root_key(armor, ORG_PASSPHRASE).public_hex == identity["root_pub"]
    assert armor_path.stat().st_mode & 0o777 == 0o600

    posts: list[dict] = []
    registry = _d21_registry(posts)
    port = _free_port()
    with _live_server(registry, port) as registry_url:
        registered = _run_with_passphrase(
            [
                "create-organization",
                "--org-armor",
                str(armor_path),
                "--registry",
                registry_url,
                "--recovery",
                "recovery-key",
            ],
            ORG_PASSPHRASE,
        )
        assert registered.returncode == 0, (
            registered.stdout + "\n" + registered.stderr
        )
        first = json.loads(registered.stdout)
        assert first["status"] == 201
        assert first["binding"]["root_pub"] == identity["root_pub"]
        assert first["binding"]["org_uuid"]
        assert first["binding"]["expires_at"] > int(time.time())
        assert first["founding"] == {
            "status": "blocked",
            "blocked_on": "auto-5dh9a",
        }
        assert first["production_registry_wiring"] == {
            "status": "blocked",
            "blocked_on": "N24",
        }
        assert "AUTONOMY NETWORK RECOVERY KEY" in first["recovery_block"]
        assert first["binding"]["org_uuid"] in first["recovery_block"]
        assert "org_uuid" not in first["envelope"]["payload"]

        repeated = _run_with_passphrase(
            [
                "create-organization",
                "--org-armor",
                str(armor_path),
                "--registry",
                registry_url,
            ],
            ORG_PASSPHRASE,
        )
        assert repeated.returncode == 0, (
            repeated.stdout + "\n" + repeated.stderr
        )
        second = json.loads(repeated.stdout)
        assert second["status"] == 200
        assert second["binding"] == first["binding"]

        posts_before_wrong_passphrase = len(posts)
        wrong = _run_with_passphrase(
            [
                "create-organization",
                "--org-armor",
                str(armor_path),
                "--registry",
                registry_url,
            ],
            "wrong org passphrase",
        )
        assert wrong.returncode != 0
        assert "wrong passphrase" in wrong.stderr
        assert len(posts) == posts_before_wrong_passphrase

        forged = dict(first["envelope"])
        forged["signer"] = KeyPair.generate().public_hex
        refused = httpx.post(
            f"{registry_url}/v1/orgs",
            json=forged,
            timeout=10,
        )
        assert refused.status_code == 403
