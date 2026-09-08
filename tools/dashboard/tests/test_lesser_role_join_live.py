"""The lesser-role join, live, with two personas — no second machine needed.

This is the capstone's central claim exercised end to end against a real
server through the EXACT modules the browser imports: the operator defines
Member with the org root, mints an invitation for it, a SECOND personal root
claims that invitation, the operator countersigns, and the admitted persona
holds no governance scope. Then Member is widened and narrowed to show both
halves of the operator's reach ruling.

What it establishes beyond the unit and jsdom layers: a joiner needs a persona
and HTTP. Not a provisioned machine, not a dashboard of its own, not an org
database — the claim routes take no operator authority because a joiner is by
definition not yet a member. The only steps that are irreducibly the
operator's are the two that need keys sealed to their personal root: defining
the role (org root) and minting the invitation (a persona holding invite:*).

Bead auto-yivxc; roles design of record graph://d1b3db8f-879.
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
VECTOR = REPO_ROOT / "tools/dashboard/static/js/ceremony/node/lesser-role-join-vector.mjs"
OPERATOR_SEED = bytes(reversed(range(32)))
JOINER_SEED = bytes((i * 7 + 3) % 251 for i in range(32))

GOVERNANCE_SCOPES = (
    "invite:member", "role:grant:member", "role:grant:*",
    "role:define", "link:publish", "membership:checkpoint", "*",
)


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


@pytest.fixture(autouse=True)
def _drained_pool():
    """This module repoints AUTONOMY_ORGS_DIR and the live server opens org
    DBs there; tools.graph.db pools by path, so drain on teardown."""
    yield
    GraphDB.close_all_pooled()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_a_second_persona_joins_as_member_and_holds_no_governance(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    slug = "lesser-role-live"
    org_id = "019c0000-0000-7000-8000-000000000303"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("GRAPH_ORG", slug)
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    org_authority._fold_cache.clear()

    GraphDB.create_org_db(slug, root=orgs_dir, org_id=org_id).close()
    root = KeyPair.generate()
    with LedgerStore(org_ledger_db_path(slug)) as store:
        founded = found_org_ledger(
            store, org_id=org_id, org_root=root,
            personal_root_seed=OPERATOR_SEED,
            now=int(time.time() * 1000) - 10_000,
        )
    org_ops._seal_org_root_setting(slug, root, OPERATOR_SEED)
    GraphDB.close_all_pooled()

    operator = derive_persona(OPERATOR_SEED, founded.genesis_id)
    joiner = derive_persona(JOINER_SEED, founded.genesis_id)
    assert joiner.public_hex != operator.public_hex, "two distinct personas"

    env = os.environ.copy()
    env["AUTONOMY_OPERATOR_SEED_HEX"] = OPERATOR_SEED.hex()
    env["AUTONOMY_JOINER_SEED_HEX"] = JOINER_SEED.hex()
    app = Starlette(routes=network_routes.ROUTES)
    with _live_server(app, _free_port()) as server_url:
        proc = subprocess.run(
            ["node", str(VECTOR), "--server", server_url,
             "--org", slug, "--genesis", founded.genesis_id],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
    assert proc.stdout.strip(), f"vector printed nothing:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result.get("ok"), f"vector failed: {result}\n{proc.stderr}"
    assert result["joinerPersona"] == joiner.public_hex

    with LedgerStore(org_ledger_db_path(slug)) as store:
        state = store.fold(now=int(time.time() * 1000))

        # The lesser role exists and ends at its ruled empty definition.
        member = state.role_defs["member"]
        assert member.scope_set == ()
        assert member.claim_requires == "admin-ack"
        assert member.approver_threshold == 1

        # THE CLAIM: a second persona is a member, holding exactly `member`.
        view = state.members.get(joiner.public_hex)
        assert view is not None, "the joiner did not become a member"
        assert set(view.roles) == {"member"}

        # THE AUTHORITY CLAIM: no governance scope, by any path.
        for scope in GOVERNANCE_SCOPES:
            assert not state.holds(joiner.public_hex, scope), scope

        # The operator still holds everything, so the org is not weakened.
        assert state.holds(operator.public_hex, "*")

        # Both halves of the reach ruling: the widen reached the holder, and
        # the narrow is a contraction the storage layer must cover.
        steps = {s["name"]: s for s in result["steps"]}
        # Admission is TWO submits: approvals reaching the threshold only make
        # the claim ready; the joiner then re-submits it at the exact staged
        # position carrying the countersignatures, and that is the admission.
        assert steps["claim_status"]["have"] == steps["claim_status"]["need"] == 1
        assert steps["finalize"]["status"] == "admitted"
        assert steps["widen"]["version"] == 2
        assert steps["narrow"]["version"] == 3
        assert steps["narrow"]["eventId"] in state.loss_heads
        assert steps["widen"]["eventId"] not in state.loss_heads
