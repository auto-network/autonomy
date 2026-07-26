"""Live acceptance for persona-signed bearer invitation issuance."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette

from tools.dashboard import network_routes, org_authority
from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair
from tools.network.idkit.armor import encrypt_root_key
from tools.network.ledger import INVITE_LIVE, LedgerStore, org_ledger_db_path
from tools.network.ledger.found import found_org_ledger


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_COMMAND = (
    REPO_ROOT
    / "tools/dashboard/static/js/ceremony/node/membership-commands.mjs"
)
PERSONAL_PASSPHRASE = "correct horse personal battery"
PERSONAL_SEED = bytes(reversed(range(32)))
OTHER_PERSONAL_SEED = bytes(range(32))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _live_server(app, port: int):
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
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


def _run_command(
    *,
    server_url: str,
    org: str,
    armor_path: Path,
    passphrase_path: Path | None,
    binding: str = "bearer",
    env_passphrase: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        "node",
        str(NODE_COMMAND),
        "issue-invitation",
        "--server",
        server_url,
        "--org",
        org,
        "--role",
        "owner",
        "--binding",
        binding,
        "--ttl-seconds",
        "3600",
        "--personal-armor",
        str(armor_path),
    ]
    env = os.environ.copy()
    env.pop("AUTONOMY_PERSONAL_PASSPHRASE", None)
    if env_passphrase is not None:
        env["AUTONOMY_PERSONAL_PASSPHRASE"] = env_passphrase
    if passphrase_path is None:
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    with passphrase_path.open("r", encoding="utf-8") as secret:
        return subprocess.run(
            command
            + ["--personal-passphrase-fd", str(secret.fileno())],
            cwd=REPO_ROOT,
            env=env,
            pass_fds=(secret.fileno(),),
            capture_output=True,
            text=True,
            timeout=30,
        )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_issues_authorized_bearer_invite_and_refuses_other_persona(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orgs_dir = tmp_path / "orgs"
    slug = "invite-live"
    org_id = "019c0000-0000-7000-8000-000000000201"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("GRAPH_ORG", slug)
    monkeypatch.delenv("GRAPH_DB", raising=False)
    org_authority._fold_cache.clear()

    database = GraphDB.create_org_db(slug, root=orgs_dir, org_id=org_id)
    database.close()
    root = KeyPair.generate()
    founded_at = int(time.time() * 1000) - 10_000
    with LedgerStore(org_ledger_db_path(slug)) as store:
        founded = found_org_ledger(
            store,
            org_id=org_id,
            org_root=root,
            personal_root_seed=PERSONAL_SEED,
            now=founded_at,
        )

    personal = KeyPair.from_private_hex(PERSONAL_SEED.hex())
    personal_armor = tmp_path / "personal.armor"
    personal_armor.write_text(
        encrypt_root_key(
            personal,
            PERSONAL_PASSPHRASE,
            iterations=10_000,
        ),
        encoding="utf-8",
    )
    passphrase_file = tmp_path / "personal.passphrase"
    passphrase_file.write_text(
        f"{PERSONAL_PASSPHRASE}\n",
        encoding="utf-8",
    )

    other = KeyPair.from_private_hex(OTHER_PERSONAL_SEED.hex())
    other_armor = tmp_path / "other-personal.armor"
    other_armor.write_text(
        encrypt_root_key(
            other,
            PERSONAL_PASSPHRASE,
            iterations=10_000,
        ),
        encoding="utf-8",
    )

    app = Starlette(routes=network_routes.ROUTES)
    with _live_server(app, _free_port()) as server_url:
        issued = _run_command(
            server_url=server_url,
            org=slug,
            armor_path=personal_armor,
            passphrase_path=passphrase_file,
        )
        assert issued.returncode == 0, issued.stdout + "\n" + issued.stderr
        result = json.loads(issued.stdout)
        assert result["binding"] == "bearer"
        assert result["role"] == "owner"
        assert result["sponsor"] == founded.founder_persona_pub
        assert len(result["token"]) == 64
        assert set(result["token"]) <= set("0123456789abcdef")

        with LedgerStore(org_ledger_db_path(slug)) as store:
            assert len(store) == 5
            invite = store.get(result["invite_id"])
            invite.verify_sig()
            assert invite.author_key == founded.founder_persona_pub
            assert invite.payload == {
                "type": "invite",
                "granted_role": "owner",
                "expiry": result["expiry"],
                "sponsor": founded.founder_persona_pub,
                "token_hash": hashlib.sha256(
                    result["token"].encode("utf-8")
                ).hexdigest(),
            }
            state = store.fold(now=result["expiry"] - 1)
            assert state.valid[invite.event_id] is True
            assert state.invites[invite.event_id] == INVITE_LIVE

        count_before_refusals = 5
        deferred = _run_command(
            server_url=server_url,
            org=slug,
            armor_path=personal_armor,
            passphrase_path=None,
            binding="key",
        )
        assert deferred.returncode != 0
        assert "deferred by D15" in deferred.stderr
        assert deferred.stdout == ""

        unauthorized = _run_command(
            server_url=server_url,
            org=slug,
            armor_path=other_armor,
            passphrase_path=None,
            env_passphrase=PERSONAL_PASSPHRASE,
        )
        assert unauthorized.returncode != 0
        assert "invitation append failed with 403" in unauthorized.stderr
        assert "lacks authority" in unauthorized.stderr
        assert unauthorized.stdout == ""

        with LedgerStore(org_ledger_db_path(slug)) as store:
            assert len(store) == count_before_refusals
            assert result["invite_id"] in store

    org_authority._fold_cache.clear()
