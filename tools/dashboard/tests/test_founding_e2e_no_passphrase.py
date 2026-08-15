"""The whole browser founding ceremony, over HTTP, with no passphrase (I1).

The three routes were built and proven one at a time; this drives all of them
in sequence the way a browser actually would -- shell, sealed root, folded
batch -- against a live server, and then asserts the two things that matter:
the organization really is founded and its root really is recoverable by its
owner, and the operator's passphrase appears nowhere on the wire.

The wire-absence claim is asserted from the bytes the client actually sent,
not from reading the client.
"""

from __future__ import annotations

import json
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
from starlette.routing import Route

from tools.graph import org_ops, settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.network_identity import (
    NETWORK_ORG_KEY_SET_ID,
    ORG_ROOT_ARMOR_PURPOSE,
)
from tools.network.idkit.keys import KeyPair
from tools.network.idkit.sealing import derive_encapsulation_keypair
from tools.network.idkit.sealing import open as seal_open
from tools.network.ledger.store import LedgerStore, org_ledger_db_path

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js" / "ceremony"
    / "tests" / "founding-e2e.mjs"
)
NOW = 1_800_000_000_000
#: The operator's real personal passphrase never enters this ceremony at all;
#: the browser works from the seed it already unlocked locally.
PERSONAL_PASSPHRASE = "an-operator-passphrase-that-must-never-travel"
PERSONAL_ROOT_SEED = bytes(reversed(range(32)))
ORG = "acme"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _live_server(app, port):
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


@pytest.fixture
def live(tmp_path, monkeypatch):
    from tools.dashboard import network_routes, server as dashboard_server

    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("GRAPH_ORG", ORG)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal", path=orgs / "personal.db").close()

    app = Starlette(routes=[
        Route("/api/orgs", dashboard_server.api_orgs_create, methods=["POST"]),
        Route(
            "/api/network/org-key/sealed",
            network_routes.post_sealed_org_key, methods=["POST"],
        ),
        Route(
            "/api/network/ledger/found",
            network_routes.post_ledger_found, methods=["POST"],
        ),
    ])
    with _live_server(app, _free_port()) as url:
        yield url
    GraphDB.close_all_pooled()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_a_browser_founds_an_organization_without_ever_sending_a_passphrase(
    live, tmp_path
):
    fixture = tmp_path / "e2e.json"
    fixture.write_text(json.dumps({
        "server_url": live,
        "org": ORG,
        "personal_root_seed_hex": PERSONAL_ROOT_SEED.hex(),
        "now": NOW,
    }))
    proc = subprocess.run(
        ["node", str(DRIVER), str(fixture)],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    )
    result = json.loads(proc.stdout)

    # ── The ceremony completed ────────────────────────────────────────────
    assert result["founded_flag"] is False, "the shell must start un-founded"
    assert len(result["event_ids"]) == 4
    assert result["genesis_id"] == result["event_ids"][0]

    # ── The ledger really is founded, with the client's own events ────────
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        assert len(store) == 4

    # ── The organization root really is recoverable by its owner ──────────
    stored = list(settings_ops.read_owned_set(NETWORK_ORG_KEY_SET_ID, org=ORG))
    assert len(stored) == 1
    payload = stored[0].payload
    assert payload["root_pub"] == result["root_pub"]
    recipient_priv, _ = derive_encapsulation_keypair(
        PERSONAL_ROOT_SEED, ORG_ROOT_ARMOR_PURPOSE
    )
    recovered = seal_open(
        bytes.fromhex(payload["sealed_root_key"]),
        recipient_priv,
        ORG_ROOT_ARMOR_PURPOSE,
    )
    assert KeyPair.from_private_hex(recovered.hex()).public_hex == result["root_pub"], (
        "the founded organization's root cannot be reopened by its owner"
    )

    # ── I1: nothing secret was ever on the wire ───────────────────────────
    sent = "\n".join(entry["sent"] for entry in result["wire"])
    assert PERSONAL_PASSPHRASE not in sent
    assert PERSONAL_ROOT_SEED.hex() not in sent, "the personal root seed travelled"
    assert recovered.hex() not in sent, "the organization root travelled in the clear"
    for field in ("password", "passphrase", "personal_password"):
        assert field not in sent, f"the client sent a {field} field"
    # It really did talk to the server -- an empty transcript would pass the
    # absence checks vacuously.
    assert [entry["route"] for entry in result["wire"]] == [
        "/api/orgs",
        "/api/network/org-key/sealed",
        "/api/network/ledger/found",
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_founding_records_which_member_this_node_is(live, tmp_path):
    """The ledger says that persona is a member; only this says it is US.

    Without it a browser-founded organization has no record of its owner on
    this node -- the seed that would derive it never leaves the browser, so it
    cannot be recovered later without the passphrase this whole change exists
    to stop asking for.
    """
    from tools.graph.schemas.network_identity import NETWORK_PERSONA_SET_ID

    fixture = tmp_path / "e2e.json"
    fixture.write_text(json.dumps({
        "server_url": live,
        "org": ORG,
        "personal_root_seed_hex": PERSONAL_ROOT_SEED.hex(),
        "now": NOW,
    }))
    proc = subprocess.run(
        ["node", str(DRIVER), str(fixture)],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    )
    result = json.loads(proc.stdout)

    # personal.db, not the org's database -- two members must never share a row.
    rows = list(settings_ops.read_owned_set(NETWORK_PERSONA_SET_ID, org=None))
    assert len(rows) == 1, "exactly one persona record for this founding"
    payload = rows[0].payload
    assert payload["persona_pub"] == result["founder_persona_pub"], (
        "the recorded persona is not the one the browser actually claimed"
    )
    assert payload["genesis_id"] == result["genesis_id"]
    assert payload["org_slug"] == ORG
    assert payload["source"] == "found"
    # And it did not ride in on the wire from the client.
    sent = "\n".join(entry["sent"] for entry in result["wire"])
    assert "persona_record" not in sent and "derived_at" not in sent
