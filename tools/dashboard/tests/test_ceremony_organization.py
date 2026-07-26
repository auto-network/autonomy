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

from tools.dashboard import network_routes
from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair
from tools.network.idkit.armor import (
    decrypt_root_key,
    encrypt_root_key,
    parse_armor,
)
from tools.network.idkit.keys import verify_signature
from tools.network.ledger.store import LedgerStore, org_ledger_db_path
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
PERSONAL_PASSPHRASE = "node personal identity armor passphrase"


def _node_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in {
            "AUTONOMY_ORG_PASSPHRASE",
            "AUTONOMY_PERSONAL_PASSPHRASE",
            "AUTONOMY_REGISTRY_URL",
        }
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


def _run_with_two_passphrases(
    arguments: list[str],
    org_passphrase: str,
    personal_passphrase: str,
) -> subprocess.CompletedProcess:
    org_read_fd, org_write_fd = os.pipe()
    personal_read_fd, personal_write_fd = os.pipe()
    try:
        os.write(org_write_fd, f"{org_passphrase}\n".encode())
        os.write(personal_write_fd, f"{personal_passphrase}\n".encode())
    finally:
        os.close(org_write_fd)
        os.close(personal_write_fd)
    try:
        return subprocess.run(
            [
                "node",
                str(COMMAND),
                *arguments,
                "--passphrase-fd",
                str(org_read_fd),
                "--personal-passphrase-fd",
                str(personal_read_fd),
            ],
            cwd=REPO,
            env=_node_environment(),
            pass_fds=(org_read_fd, personal_read_fd),
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        os.close(org_read_fd)
        os.close(personal_read_fd)


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


def _dashboard_founding_app(expected_headers: list[str]) -> Starlette:
    async def get_org(request: Request):
        slug = request.path_params["slug"]
        expected_headers.append(request.headers.get("x-graph-org"))
        from tools.graph import org_ops

        org = org_ops.get_org(slug)
        if org is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"org": org.to_dict(), "identity": None})

    return Starlette(routes=[
        Route("/api/orgs/{slug}", get_org, methods=["GET"]),
        *network_routes.ROUTES,
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
def test_node_commands_found_locally_and_optionally_register(
    tmp_path,
    monkeypatch,
):
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    slug = "headless-created"
    org_id = "019c0000-0000-7000-8000-000000000101"
    org_db = GraphDB.create_org_db(slug, root=orgs_dir, org_id=org_id)
    org_db.close()
    monkeypatch.setenv("GRAPH_ORG", slug)

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

    personal = KeyPair.from_private_hex(bytes(reversed(range(32))).hex())
    personal_armor_path = tmp_path / "personal-root.armor"
    personal_armor_path.write_text(
        encrypt_root_key(
            personal,
            PERSONAL_PASSPHRASE,
            iterations=10_000,
        )
        + "\n",
        encoding="utf-8",
    )

    posts: list[dict] = []
    preflight_headers: list[str] = []
    registry = _d21_registry(posts)
    dashboard = _dashboard_founding_app(preflight_headers)
    registry_port = _free_port()
    dashboard_port = _free_port()
    with (
        _live_server(registry, registry_port) as registry_url,
        _live_server(dashboard, dashboard_port) as dashboard_url,
    ):
        registered = _run_with_two_passphrases(
            [
                "create-organization",
                "--org-armor",
                str(armor_path),
                "--personal-armor",
                str(personal_armor_path),
                "--server",
                dashboard_url,
                "--org",
                slug,
                "--registry",
                registry_url,
                "--recovery",
                "recovery-key",
            ],
            ORG_PASSPHRASE,
            PERSONAL_PASSPHRASE,
        )
        assert registered.returncode == 0, (
            registered.stdout + "\n" + registered.stderr
        )
        first = json.loads(registered.stdout)
        assert first["status"] == 201
        assert first["binding"]["root_pub"] == identity["root_pub"]
        assert first["binding"]["org_uuid"]
        assert first["binding"]["expires_at"] > int(time.time())
        assert first["founding"]["ok"] is True
        assert first["founding"]["genesis_id"]
        assert first["founding"]["founder_persona_pub"]
        assert len(first["founding"]["event_ids"]) == 4
        assert first["production_registry_wiring"] == {
            "status": "blocked",
            "blocked_on": "N24",
        }
        assert "AUTONOMY NETWORK RECOVERY KEY" in first["recovery_block"]
        assert first["binding"]["org_uuid"] in first["recovery_block"]
        assert "org_uuid" not in first["envelope"]["payload"]
        assert preflight_headers == [slug]

        with LedgerStore(org_ledger_db_path(slug)) as store:
            assert len(store) == 4
            state = store.fold()
            founder = first["founding"]["founder_persona_pub"]
            assert state.authority(founder) == frozenset({"*"})

        repeated = httpx.post(
            f"{registry_url}/v1/orgs",
            json=first["envelope"],
            timeout=10,
        )
        assert repeated.status_code == 200
        assert repeated.json() == first["binding"]

        posts_before_wrong_passphrase = len(posts)
        wrong_slug = "wrong-passphrase-org"
        wrong_org_id = "019c0000-0000-7000-8000-000000000102"
        wrong_db = GraphDB.create_org_db(
            wrong_slug,
            root=orgs_dir,
            org_id=wrong_org_id,
        )
        wrong_db.close()
        monkeypatch.setenv("GRAPH_ORG", wrong_slug)
        wrong = _run_with_two_passphrases(
            [
                "create-organization",
                "--org-armor",
                str(armor_path),
                "--personal-armor",
                str(personal_armor_path),
                "--server",
                dashboard_url,
                "--org",
                wrong_slug,
                "--registry",
                registry_url,
            ],
            "wrong org passphrase",
            PERSONAL_PASSPHRASE,
        )
        assert wrong.returncode != 0
        assert "wrong passphrase" in wrong.stderr
        assert len(posts) == posts_before_wrong_passphrase
        # The authority ledger is co-located with the pre-existing org
        # database. A bad passphrase must leave it unfounded, not delete
        # the organization's metadata database.
        with LedgerStore(org_ledger_db_path(wrong_slug)) as store:
            assert len(store) == 0

        wrong_personal_slug = "wrong-personal-passphrase-org"
        wrong_personal_org_id = "019c0000-0000-7000-8000-000000000104"
        wrong_personal_db = GraphDB.create_org_db(
            wrong_personal_slug,
            root=orgs_dir,
            org_id=wrong_personal_org_id,
        )
        wrong_personal_db.close()
        monkeypatch.setenv("GRAPH_ORG", wrong_personal_slug)
        wrong_personal = _run_with_two_passphrases(
            [
                "create-organization",
                "--org-armor",
                str(armor_path),
                "--personal-armor",
                str(personal_armor_path),
                "--server",
                dashboard_url,
                "--org",
                wrong_personal_slug,
                "--registry",
                registry_url,
            ],
            ORG_PASSPHRASE,
            "wrong personal passphrase",
        )
        assert wrong_personal.returncode != 0
        assert "wrong passphrase" in wrong_personal.stderr
        assert len(posts) == posts_before_wrong_passphrase
        with LedgerStore(org_ledger_db_path(wrong_personal_slug)) as store:
            assert len(store) == 0

        forged = dict(first["envelope"])
        forged["signer"] = KeyPair.generate().public_hex
        refused = httpx.post(
            f"{registry_url}/v1/orgs",
            json=forged,
            timeout=10,
        )
        assert refused.status_code == 403
        posts_before_local_founding = len(posts)

        local_slug = "local-only"
        local_org_id = "019c0000-0000-7000-8000-000000000103"
        local_db = GraphDB.create_org_db(
            local_slug,
            root=orgs_dir,
            org_id=local_org_id,
        )
        local_db.close()
        monkeypatch.setenv("GRAPH_ORG", local_slug)
        local_armor_path = tmp_path / "local-org-root.armor"
        local_identity = _run_with_passphrase(
            [
                "create-org-identity",
                "--armor-output",
                str(local_armor_path),
            ],
            ORG_PASSPHRASE,
        )
        assert local_identity.returncode == 0, (
            local_identity.stdout + "\n" + local_identity.stderr
        )
        local = _run_with_two_passphrases(
            [
                "create-organization",
                "--org-armor",
                str(local_armor_path),
                "--personal-armor",
                str(personal_armor_path),
                "--server",
                dashboard_url,
                "--org",
                local_slug,
            ],
            ORG_PASSPHRASE,
            PERSONAL_PASSPHRASE,
        )
        assert local.returncode == 0, local.stdout + "\n" + local.stderr
        local_result = json.loads(local.stdout)
        assert local_result["registration"] is None
        assert local_result["founding"]["ok"] is True
        assert len(posts) == posts_before_local_founding
        with LedgerStore(org_ledger_db_path(local_slug)) as local_store:
            assert len(local_store) == 4
