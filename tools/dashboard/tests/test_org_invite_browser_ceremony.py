"""The Membership screen's mint ceremony, node-executed against a real ledger.

Runs the EXACT module the browser imports (org-invite.js, via
org-invite-vector.mjs) against a live server over a real founded ledger — the
cross-implementation proof the operator requires before any ceremony reaches
a passkey. Bead auto-aopjw; harness cloned from test_invite_issuance.py.
"""
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
from tools.network.idkit.persona import derive_persona
from tools.network.ledger import INVITE_LIVE, LedgerStore, org_ledger_db_path
from tools.network.ledger.found import found_org_ledger


REPO_ROOT = Path(__file__).resolve().parents[3]
VECTOR = (
    REPO_ROOT
    / "tools/dashboard/static/js/ceremony/node/org-invite-vector.mjs"
)
PERSONAL_SEED = bytes(reversed(range(32)))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _live_server(app, port: int):
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_mint_module_appends_a_live_invite(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    slug = "mint-live"
    org_id = "019c0000-0000-7000-8000-000000000301"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("GRAPH_ORG", slug)
    monkeypatch.delenv("GRAPH_DB", raising=False)
    org_authority._fold_cache.clear()

    GraphDB.create_org_db(slug, root=orgs_dir, org_id=org_id).close()
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

    expiry = int(time.time() * 1000) + 7 * 86_400_000
    app = Starlette(routes=network_routes.ROUTES)
    env = os.environ.copy()
    env["AUTONOMY_PERSONAL_SEED_HEX"] = PERSONAL_SEED.hex()
    with _live_server(app, _free_port()) as server_url:
        proc = subprocess.run(
            [
                "node", str(VECTOR),
                "--server", server_url,
                "--org", slug,
                "--genesis", founded.genesis_id,
                "--role", "owner",
                "--expiry", str(expiry),
                "--max-uses", "5",
            ],
            cwd=REPO_ROOT, env=env,
            capture_output=True, text=True, timeout=60,
        )
    assert proc.returncode == 0, f"vector failed:\n{proc.stdout}\n{proc.stderr}"
    result = json.loads(proc.stdout)

    with LedgerStore(org_ledger_db_path(slug)) as store:
        state = store.fold(now=int(time.time() * 1000))
        invite_id = result["inviteId"]
        assert state.invites[invite_id] == INVITE_LIVE
        assert state.invite_uses[invite_id] == {
            "max_uses": 5, "used": 0, "remaining": 5,
        }
        event = store.get(invite_id)
        founder = derive_persona(PERSONAL_SEED, founded.genesis_id)
        assert event.payload["sponsor"] == founder.public_hex
        assert event.payload["expiry"] == expiry
        # The bearer never crossed the wire: only its hash is in the event,
        # and the vector's local token matches it.
        assert event.payload["token_hash"] == hashlib.sha256(
            result["bearer"].encode("utf-8")
        ).hexdigest()
