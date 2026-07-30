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
                     registry_url: str, requests: list[str],
                     genesis_id: str | None = None,
                     personal_armor: str | None = None,
                     personal_root_pub: str | None = None) -> Starlette:
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

    async def ledger_heads(request: Request):
        requests.append(str(request.url.path))
        if request.query_params.get("org") != ORG_SLUG or genesis_id is None:
            return JSONResponse(
                {"ok": False, "error": "organization ledger is not founded"},
                status_code=404,
            )
        return JSONResponse({"genesis_id": genesis_id, "heads": [genesis_id]})

    async def personal(request: Request):
        requests.append(str(request.url.path))
        if personal_armor is None:
            return JSONResponse(
                {"error": "no personal identity"}, status_code=404,
            )
        return JSONResponse({
            "armored_private_key": personal_armor,
            "root_pub": personal_root_pub,
        })

    return Starlette(routes=[
        Route("/api/network/org-key", org_key),
        Route("/api/network/binding", binding),
        Route("/api/network/ledger/heads", ledger_heads),
        Route("/api/identity/personal", personal),
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
        # The unfounded ledger (heads → 404) short-circuits the persona
        # resolution before the personal armor is ever fetched, and the
        # cert falls back to the legacy label subject.
        assert identity_requests == [
            "/api/network/org-key",
            "/api/network/binding",
            "/api/network/ledger/heads",
        ]
        legacy_subject = json.loads(output["envelope"]["cert"])["subject"]
        assert legacy_subject["kind"] == "operator"
        assert legacy_subject["id"].startswith("browser-")

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


def _publish_run(*, root: KeyPair, org_uuid: str, passphrase: str,
                 identity_kwargs: dict) -> tuple[dict, list[str]]:
    """Stand up registry + identity source, register the org, run the
    command with *passphrase*, and return (parsed output, identity trace)."""
    registry_port = _free_port()
    registry_url = f"http://127.0.0.1:{registry_port}"
    registry = create_app(
        ":memory:",
        base_url=registry_url,
        secure_cookies=False,
    )
    identity_requests: list[str] = []
    identity_port = _free_port()
    identity = _identity_source(
        armor=encrypt_root_key(root, passphrase, iterations=10_000),
        root_pub=root.public_hex,
        org_uuid=org_uuid,
        registry_url=registry_url,
        requests=identity_requests,
        **identity_kwargs,
    )
    with (
        _live_server(registry, registry_port),
        _live_server(identity, identity_port) as identity_url,
    ):
        registration = httpx.post(
            f"{registry_url}/v1/orgs",
            json=sign_request(
                root,
                "POST",
                "/v1/orgs",
                {
                    "org_uuid": org_uuid,
                    "root_pub": root.public_hex,
                    "recovery_policy": "none",
                },
                ts=int(time.time()),
            ),
            timeout=10,
        )
        assert registration.status_code == 201, registration.text
        command = _run_with_passphrase(identity_url, passphrase)
    assert command.returncode == 0, command.stdout + "\n" + command.stderr
    return json.loads(command.stdout), identity_requests


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_signon_mints_persona_subject_for_a_founded_org():
    """D13: with a founded ledger and a personal identity whose armor the
    entered passphrase opens, the session cert names the derived persona
    as its subject — byte-identical to idkit's derivation — and the
    registry accepts the resulting envelope unchanged."""
    from tools.network.idkit.persona import derive_persona

    root = KeyPair.generate()
    personal_root = KeyPair.generate()
    passphrase = "one passphrase opens both armors"
    genesis_id = "c0" * 32
    output, identity_requests = _publish_run(
        root=root,
        org_uuid=str(uuid.uuid4()),
        passphrase=passphrase,
        identity_kwargs={
            "genesis_id": genesis_id,
            "personal_armor": encrypt_root_key(
                personal_root, passphrase, iterations=10_000,
            ),
            "personal_root_pub": personal_root.public_hex,
        },
    )
    expected = derive_persona(
        bytes.fromhex(personal_root.private_hex), genesis_id,
    ).public_hex
    assert output["status"] == 201
    # Kind stays 'operator' on the rung-1 HTTP transport (the registry
    # 501s kind 'persona'); the persona rides in subject.id, which is
    # what the dashboard's fold-based gate authorizes.
    assert json.loads(output["envelope"]["cert"])["subject"] == {
        "kind": "operator",
        "id": expected,
    }
    assert identity_requests == [
        "/api/network/org-key",
        "/api/network/binding",
        "/api/network/ledger/heads",
        "/api/identity/personal",
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_signon_falls_back_to_label_when_personal_armor_stays_shut():
    """A passphrase that opens the org armor but not the personal armor
    must not fail sign-on: the cert falls back to the legacy label
    subject (and the founded ledger changes nothing about that)."""
    root = KeyPair.generate()
    personal_root = KeyPair.generate()
    passphrase = "opens only the org armor"
    output, identity_requests = _publish_run(
        root=root,
        org_uuid=str(uuid.uuid4()),
        passphrase=passphrase,
        identity_kwargs={
            "genesis_id": "d1" * 32,
            "personal_armor": encrypt_root_key(
                personal_root, "a different personal password",
                iterations=10_000,
            ),
            "personal_root_pub": personal_root.public_hex,
        },
    )
    assert output["status"] == 201
    subject = json.loads(output["envelope"]["cert"])["subject"]
    assert subject["kind"] == "operator"
    assert subject["id"].startswith("browser-")
    assert identity_requests == [
        "/api/network/org-key",
        "/api/network/binding",
        "/api/network/ledger/heads",
        "/api/identity/personal",
    ]
