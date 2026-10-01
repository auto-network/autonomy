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
from types import SimpleNamespace

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
    """A live server mounting what the vault-bearing founding calls (auto-i15c4).

    Founding opens the sealed sign-in preparation, stores the sealed root,
    folds the batch and hands the new organization's recovery and storage
    delegate to the vault. Those handlers are the real ones; the personal
    identity they need is derived from PERSONAL_ROOT_SEED, the way
    test_org_vault_signon_write.py sets it up. Registration and set-up are not
    mounted: founding reports their failure and never throws on it.
    """
    from starlette.responses import JSONResponse

    from tools.dashboard import (
        fleet_enrollment_routes, identity_routes, link_serving_supervisor,
        network_routes, server as dashboard_server, service_certificate_manager,
        signon_preparation, unlock_routes, vault_routes,
    )
    from tools.graph.schemas.vault_policy_class import VAULT_POLICY_CLASS_SET_ID
    from tools.vault.key_holder import _scoped_db
    from tools.vault.personal_object import derive_delegate_audited_recipient
    from tools.vault.store import VaultStore

    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("GRAPH_ORG", ORG)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    # Pinned, not inherited. The sealed-key route is operator-only
    # (auto-6ff9b) and this app mounts no identity middleware, so its requests
    # are compatibility traffic that reaches the handler only through the
    # unenforced-gate stand-down. Stating it keeps this a test of the FOUNDING
    # SEQUENCE: the authorization contract is
    # test_org_key_routes_require_operator.py, and if that stand-down ever
    # changes these should fail loudly rather than quietly swap meaning.
    monkeypatch.setattr(unlock_routes, "gate_enforced", lambda: False)
    monkeypatch.setattr(unlock_routes, "session_from_request", lambda request: {"sid": "test"})
    # Empty process vault state: nothing pre-supplied by the test.
    monkeypatch.setattr(unlock_routes, "_VAULT_CACHE", {})
    monkeypatch.setattr(settings_ops, "_vault_sealer", None)
    monkeypatch.setattr(settings_ops, "_vault_key_holder", None)
    monkeypatch.setattr(settings_ops, "_personal_delegate_audited_key", None)
    monkeypatch.setattr(unlock_routes, "save_vault_across_hot_reload", lambda: False)
    monkeypatch.setattr(service_certificate_manager, "request_reconcile", lambda: None)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal", path=orgs / "personal.db").close()

    # The operator's personal identity, derived from the seed the browser
    # unlocks: its root and the audited recipient the preparation seals to.
    root_pub = KeyPair.from_private_hex(PERSONAL_ROOT_SEED.hex()).public_hex
    _, audited_public = derive_delegate_audited_recipient(PERSONAL_ROOT_SEED)
    with VaultStore(_scoped_db(VAULT_POLICY_CLASS_SET_ID, None)) as store:
        store.put_delegate_audited_recipient(audited_public)
    monkeypatch.setattr(identity_routes, "_personal_member",
                        lambda: SimpleNamespace(payload={"root_pub": root_pub}))
    # Personal policy inventory, fleet runtime and serving certificates are
    # outside the founding sequence.
    monkeypatch.setattr(vault_routes, "root_anchor_inventory", lambda: {
        "anchors": [], "classes": [{"governance": {"form": "root-reachable"}}]})
    monkeypatch.setattr(fleet_enrollment_routes, "runtime_preparation",
                        lambda: JSONResponse({"enabled": False}))
    monkeypatch.setattr(fleet_enrollment_routes, "_local_completion_state",
                        lambda: (None, None, None))
    monkeypatch.setattr(link_serving_supervisor, "serve_cert_state", lambda org: {})
    monkeypatch.setattr(link_serving_supervisor, "serve_cert_requirement",
                        lambda org: {"required": False})

    async def existing_personal_class(request):
        return JSONResponse({"anchors": [], "classes": [{
            "governance": {"form": "root-reachable"},
        }]})

    async def reported(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[
        Route("/api/orgs", dashboard_server.api_orgs_create, methods=["POST"]),
        Route("/api/identity/unlock/preparation", signon_preparation.get_preparation),
        Route("/api/identity/ceremony-error", reported, methods=["POST"]),
        Route("/api/identity/vault-anchors", existing_personal_class),
        Route(
            "/api/network/org-key/sealed",
            network_routes.post_sealed_org_key, methods=["POST"],
        ),
        Route(
            "/api/network/ledger/found",
            network_routes.post_ledger_found, methods=["POST"],
        ),
        Route("/api/identity/unlock/vault-keys",
              unlock_routes.post_unlock_vault_keys, methods=["POST"]),
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
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)

    # ── The ceremony completed ────────────────────────────────────────────
    assert result["founded_flag"] is False, "the shell must start un-founded"
    assert len(result["event_ids"]) == 4
    assert result["genesis_id"] == result["event_ids"][0]

    # ── The ledger really is founded, with the client's own events ────────
    # The four founding events, then the one event the vault handoff appends:
    # the founder delegating storage to this node (vault-bearing founding,
    # auto-2vseu). Nothing else may be written.
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        events = store.events()
    ids = [event.event_id for event in events]
    assert len(ids) == 5, [event.payload.get("type") for event in events]
    assert set(result["event_ids"]) <= set(ids)
    extra = [event for event in events if event.event_id not in result["event_ids"]]
    assert len(extra) == 1 and "delegat" in json.dumps(extra[0].payload), extra[0].payload

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
    # The founding sequence, in order: shell, sealed sign-in preparation,
    # sealed root, folded batch, vault handoff. Registration and set-up follow
    # but are not mounted here; founding reports their failure without throwing.
    routes = [entry["route"] for entry in result["wire"]]
    sequence = [
        "/api/orgs",
        "/api/identity/unlock/preparation",
        "/api/network/org-key/sealed",
        "/api/network/ledger/found",
        "/api/identity/unlock/vault-keys",
    ]
    positions = [routes.index(route) for route in sequence]
    assert positions == sorted(positions), routes
    for entry in result["wire"]:
        if entry["route"] in sequence:
            assert entry["status"] < 300, entry["route"]


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
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)

    # personal.db, not the org's database -- two members must never share a row.
    rows = list(settings_ops.read_owned_set(NETWORK_PERSONA_SET_ID, org=None))
    assert len(rows) == 1, "exactly one persona record for this founding"
    payload = rows[0].payload
    assert payload["persona_pub"] == result["founder_persona_pub"], (
        "the recorded persona is not the one the browser actually claimed"
    )
    # Keyed by the organization's genesis, not repeated in the payload
    # (049e01f7): the genesis is what binds this persona to ORG's ledger.
    assert rows[0].key == result["genesis_id"]
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        assert result["genesis_id"] in {event.event_id for event in store.events()}
    assert payload["source"] == "found"
    # And it did not ride in on the wire from the client.
    sent = "\n".join(entry["sent"] for entry in result["wire"])
    assert "persona_record" not in sent and "derived_at" not in sent
