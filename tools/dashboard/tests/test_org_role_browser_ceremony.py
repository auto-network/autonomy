"""The Roles editor's ceremonies, node-executed against a real ledger.

Runs the EXACT module the browser imports (org-role.js, via
org-role-vector.mjs) against a live server over a real founded ledger whose
org root was sealed by the Python founding path — the cross-implementation
proof that the unseal + sign chain holds before any operator passkey touches
it. Bead auto-oazv4; harness cloned from test_org_invite_browser_ceremony.py.
"""
from __future__ import annotations

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
from tools.graph import org_ops
from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair
from tools.network.idkit.persona import derive_persona
from tools.network.ledger import LedgerStore, org_ledger_db_path
from tools.network.ledger.found import found_org_ledger


REPO_ROOT = Path(__file__).resolve().parents[3]
VECTOR = REPO_ROOT / "tools/dashboard/static/js/ceremony/node/org-role-vector.mjs"
PERSONAL_SEED = bytes(reversed(range(32)))
STRANGER_SEED = bytes(range(32))


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


def _vector(server_url: str, seed: bytes, *args: str) -> dict:
    env = os.environ.copy()
    env["AUTONOMY_PERSONAL_SEED_HEX"] = seed.hex()
    proc = subprocess.run(
        ["node", str(VECTOR), "--server", server_url, *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.stdout.strip(), f"vector printed nothing:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    result["_returncode"] = proc.returncode
    return result


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_role_module_defines_grants_and_revokes_on_a_live_ledger(
    tmp_path, monkeypatch,
):
    orgs_dir = tmp_path / "orgs"
    slug = "roles-live"
    org_id = "019c0000-0000-7000-8000-000000000302"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("GRAPH_ORG", slug)
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    org_authority._fold_cache.clear()

    GraphDB.create_org_db(slug, root=orgs_dir, org_id=org_id).close()
    root = KeyPair.generate()
    founded_at = int(time.time() * 1000) - 10_000
    with LedgerStore(org_ledger_db_path(slug)) as store:
        founded = found_org_ledger(
            store, org_id=org_id, org_root=root,
            personal_root_seed=PERSONAL_SEED, now=founded_at,
        )
    # The founding ceremony's seal: the org root, sealed to the founder's
    # personal-root-derived encapsulation key. The browser opens exactly this.
    org_ops._seal_org_root_setting(slug, root, PERSONAL_SEED)
    GraphDB.close_all_pooled()
    founder = derive_persona(PERSONAL_SEED, founded.genesis_id)
    target = KeyPair.generate().public_hex

    app = Starlette(routes=network_routes.ROUTES)
    with _live_server(app, _free_port()) as server_url:
        # 1. Define Member with the org root, version left for the module
        #    to settle (a new name starts at 1).
        defined = _vector(server_url, PERSONAL_SEED, "--org", slug,
                          "--action", "define", "--name", "member",
                          "--threshold", "1")
        assert defined["_returncode"] == 0, defined
        assert defined["version"] == 1
        assert defined["rootPub"] == root.public_hex

        # 2. Widen Member without pinning a version: the route answers
        #    expected_version=2 and the module re-signs at 2.
        widened = _vector(server_url, PERSONAL_SEED, "--org", slug,
                          "--action", "define", "--name", "member",
                          "--scopes", "invite:member")
        assert widened["_returncode"] == 0, widened
        assert widened["version"] == 2

        # 3. The founder persona (owner, holds *) grants Member to a key.
        granted = _vector(server_url, PERSONAL_SEED, "--org", slug,
                          "--action", "grant", "--genesis", founded.genesis_id,
                          "--persona", target, "--role", "member")
        assert granted["_returncode"] == 0, granted
        assert granted["signer"] == founder.public_hex

        # 4. A stranger's persona cannot grant: the fold's reason surfaces
        #    on the thrown error, and nothing is appended.
        refused = _vector(server_url, STRANGER_SEED, "--org", slug,
                          "--action", "grant", "--genesis", founded.genesis_id,
                          "--persona", target, "--role", "member")
        assert refused["_returncode"] == 1
        assert refused["status"] == 403
        assert refused["reason"] == "role-grant-unauthorized"

        # 5. The founder revokes it again.
        revoked = _vector(server_url, PERSONAL_SEED, "--org", slug,
                          "--action", "revoke", "--genesis", founded.genesis_id,
                          "--persona", target, "--role", "member")
        assert revoked["_returncode"] == 0, revoked

    with LedgerStore(org_ledger_db_path(slug)) as store:
        state = store.fold(now=int(time.time() * 1000))
        member = state.role_defs["member"]
        assert member.version == 2
        assert member.scope_set == ("invite:member",)
        assert member.claim_requires == "admin-ack"
        assert state.roles(target) == ()          # granted, then revoked
        assert store.get(defined["eventId"]).author_key == root.public_hex
        assert store.get(granted["eventId"]).author_key == founder.public_hex
        assert state.valid[granted["eventId"]] and state.valid[revoked["eventId"]]
        # The refused grant never became a row.
        assert all(
            e.type != "role.grant" or e.author_key == founder.public_hex
            for e in store.ledger.events()
        )
