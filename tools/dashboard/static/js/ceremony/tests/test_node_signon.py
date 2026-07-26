"""Live-path proof for the shared Node sign-on command."""

from __future__ import annotations

import copy
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
from tools.network.idkit.armor import encrypt_root_key
from tools.network.registry.app import create_app
from tools.network.registry.signing import sign_request

REPO = Path(__file__).resolve().parents[6]
COMMAND = (
    REPO
    / "tools" / "dashboard" / "static" / "js"
    / "ceremony" / "node" / "signon.mjs"
)
ORG_SLUG = "headless-live-org"
TARGET = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


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
    assert server.started, f"server on port {port} did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), f"server on port {port} did not stop"


def _identity_source(*, armor: str, root_pub: str, org_uuid: str,
                     registry_url: str, requests: list[str]) -> Starlette:
    async def org_key(request: Request):
        requests.append(str(request.url.path))
        if request.query_params.get("org") != ORG_SLUG:
            return JSONResponse({"error": "unknown org"}, status_code=404)
        return JSONResponse({
            "label": "default",
            "armored_private_key": armor,
            "root_pub": root_pub,
        })

    async def binding(request: Request):
        requests.append(str(request.url.path))
        if request.query_params.get("org") != ORG_SLUG:
            return JSONResponse({"error": "unknown org"}, status_code=404)
        return JSONResponse({
            "org_uuid": org_uuid,
            "root_pub": root_pub,
            "registry_url": registry_url,
        })

    return Starlette(routes=[
        Route("/api/network/org-key", org_key),
        Route("/api/network/binding", binding),
    ])


def _passphrase_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key != "AUTONOMY_ORG_PASSPHRASE"
    }


def _run_with_passphrase(identity_url: str, passphrase: str) -> subprocess.CompletedProcess:
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
                "--server",
                identity_url,
                "--org",
                ORG_SLUG,
                "--target",
                TARGET,
                "--ttl",
                "3600",
                "--passphrase-fd",
                str(read_fd),
            ],
            cwd=REPO,
            env=_passphrase_environment(),
            pass_fds=(read_fd,),
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        os.close(read_fd)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_signon_is_accepted_and_tampering_is_rejected():
    root = KeyPair.generate()
    org_uuid = str(uuid.uuid4())
    passphrase = "headless live sign-on passphrase"
    armor = encrypt_root_key(root, passphrase, iterations=10_000)

    registry_port = _free_port()
    registry_url = f"http://127.0.0.1:{registry_port}"
    registry_posts: list[str] = []
    registry = create_app(
        ":memory:",
        base_url=registry_url,
        secure_cookies=False,
    )

    @registry.middleware("http")
    async def count_link_posts(request: Request, call_next):
        if request.method == "POST" and request.url.path == "/v1/links":
            registry_posts.append(request.url.path)
        return await call_next(request)

    identity_requests: list[str] = []
    identity_port = _free_port()
    identity = _identity_source(
        armor=armor,
        root_pub=root.public_hex,
        org_uuid=org_uuid,
        registry_url=registry_url,
        requests=identity_requests,
    )

    with (
        _live_server(registry, registry_port),
        _live_server(identity, identity_port) as identity_url,
    ):
        registration_payload = {
            "org_uuid": org_uuid,
            "root_pub": root.public_hex,
            "recovery_policy": "none",
        }
        registration = httpx.post(
            f"{registry_url}/v1/orgs",
            json=sign_request(
                root,
                "POST",
                "/v1/orgs",
                registration_payload,
                ts=int(time.time()),
            ),
            timeout=10,
        )
        assert registration.status_code == 201, registration.text

        command = _run_with_passphrase(identity_url, passphrase)
        assert command.returncode == 0, command.stdout + "\n" + command.stderr
        output = json.loads(command.stdout)
        assert output["status"] == 201
        assert output["registry"]["token"]
        assert output["registry"]["url"].endswith(
            f"/l/{output['registry']['token']}"
        )
        assert output["signOn"]["org"] == org_uuid
        assert output["signOn"]["rootPub"] == root.public_hex
        assert output["request"] == {
            "method": "POST",
            "path": "/v1/links",
            "payload": {
                "org": org_uuid,
                "target_uuid": TARGET,
                "target_type": "present",
            },
        }
        assert registry_posts == ["/v1/links"]
        assert identity_requests == [
            "/api/network/org-key",
            "/api/network/binding",
        ]

        signature_mutation = copy.deepcopy(output["envelope"])
        signature_mutation["sig"] = (
            ("0" if signature_mutation["sig"][0] != "0" else "1")
            + signature_mutation["sig"][1:]
        )
        bad_signature = httpx.post(
            f"{registry_url}/v1/links",
            json=signature_mutation,
            timeout=10,
        )
        assert 400 <= bad_signature.status_code < 500, bad_signature.text

        payload_mutation = copy.deepcopy(output["envelope"])
        payload_mutation["payload"]["target_uuid"] = str(uuid.uuid4())
        bad_payload = httpx.post(
            f"{registry_url}/v1/links",
            json=payload_mutation,
            timeout=10,
        )
        assert 400 <= bad_payload.status_code < 500, bad_payload.text

        posts_before_missing_passphrase = len(registry_posts)
        identity_reads_before_missing_passphrase = len(identity_requests)
        missing_passphrase = subprocess.run(
            [
                "node",
                str(COMMAND),
                "--server",
                identity_url,
                "--org",
                ORG_SLUG,
            ],
            cwd=REPO,
            env=_passphrase_environment(),
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert missing_passphrase.returncode != 0
        assert "missing passphrase source" in missing_passphrase.stderr
        assert len(registry_posts) == posts_before_missing_passphrase
        assert len(identity_requests) == identity_reads_before_missing_passphrase
