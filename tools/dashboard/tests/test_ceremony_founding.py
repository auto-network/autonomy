"""Live client-signed organization founding acceptance."""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette

from tools.dashboard import network_routes
from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair, canonical_json
from tools.network.idkit.sealing import derive_encapsulation_keypair
from tools.network.ledger.found import found_org_ledger
from tools.network.ledger.store import LedgerStore, org_ledger_db_path
from tools.network.storagekit.credentials import kem_purpose, validate

REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_DRIVER = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js"
    / "ceremony" / "tests" / "founding-live.mjs"
)
NOW = 1_800_000_000_000
PERSONAL_ROOT_SEED = bytes(reversed(range(32)))
KEM_SEED = bytes(range(32, 64))


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


def _create_org(orgs_dir: Path, slug: str, org_id: str) -> None:
    database = GraphDB.create_org_db(
        slug,
        root=orgs_dir,
        org_id=org_id,
    )
    database.close()


def _org_metadata(path: Path) -> dict:
    """Snapshot non-ledger identity/content rows and the graph schema stamp."""
    with sqlite3.connect(path) as database:
        return {
            "user_version": database.execute(
                "PRAGMA user_version"
            ).fetchone()[0],
            "orgs": database.execute(
                "SELECT * FROM orgs ORDER BY slug"
            ).fetchall(),
            "sources": database.execute(
                "SELECT * FROM sources ORDER BY id"
            ).fetchall(),
        }


def _run_node(tmp_path: Path, **fixture) -> dict:
    fixture_path = tmp_path / f"{fixture['org']}-founding.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    result = subprocess.run(
        ["node", str(NODE_DRIVER), str(fixture_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_founding_posts_atomic_batch_and_folds_owner(
    tmp_path,
    monkeypatch,
):
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    root = KeyPair.from_private_hex(bytes(range(32)).hex())
    app = Starlette(routes=network_routes.ROUTES)
    port = _free_port()

    slug = "node-founded"
    org_id = "019c0000-0000-7000-8000-000000000001"
    _create_org(orgs_dir, slug, org_id)
    store_path = org_ledger_db_path(slug)
    metadata_before_founding = _org_metadata(store_path)
    monkeypatch.setenv("GRAPH_ORG", slug)

    with _live_server(app, port) as server_url:
        founded = _run_node(
            tmp_path,
            org=slug,
            org_id=org_id,
            root_seed_hex=root.private_hex,
            root_pub=root.public_hex,
            personal_root_seed_hex=PERSONAL_ROOT_SEED.hex(),
            now=NOW,
            server_url=server_url,
        )

        assert founded["server"] == {
            "ok": True,
            "genesis_id": founded["genesisId"],
            "event_ids": founded["eventIds"],
        }
        assert founded["kemCredential"] is None
        assert founded["kemPrivateKey"] is None

        with LedgerStore() as reference_store:
            reference = found_org_ledger(
                reference_store,
                org_id=org_id,
                org_root=root,
                personal_root_seed=PERSONAL_ROOT_SEED,
                now=NOW,
            )
            reference_ids = [
                reference.genesis_id,
                reference.role_define_id,
                reference.founding_invite_id,
                reference.founder_claim_id,
            ]
            assert founded["eventIds"] == reference_ids
            assert founded["events"] == [
                reference_store.get(event_id).to_dict()
                for event_id in reference_ids
            ]

        with LedgerStore(store_path) as store:
            assert len(store) == 4
            events = [store.get(event_id) for event_id in founded["eventIds"]]
            assert [event.type for event in events] == [
                "genesis",
                "role.define",
                "invite",
                "member.claim",
            ]
            assert [event.event_id for event in events] == founded["eventIds"]
            assert events[0].payload["org"] == org_id
            assert events[1].payload == {
                "type": "role.define",
                "name": "owner",
                "scope_set": ["*"],
                "claim_requires": "self",
                "version": 1,
            }
            assert events[2].payload["invite_pub"] == founded["founderPersonaPub"]
            assert events[3].payload["persona_pub"] == founded["founderPersonaPub"]
            assert "kem_credential" not in events[3].payload
            state = store.fold(now=NOW)
            founder = state.members[founded["founderPersonaPub"]]
            assert founder.roles == ("owner",)
            assert state.authority(founded["founderPersonaPub"]) == frozenset({"*"})
            assert state.holds(founded["founderPersonaPub"], "link:publish")
        assert _org_metadata(store_path) == metadata_before_founding
        reopened = GraphDB.open_org_db(slug, root=orgs_dir, mode="ro")
        try:
            org_row = reopened.conn.execute(
                "SELECT id, slug FROM orgs"
            ).fetchone()
            assert tuple(org_row) == (org_id, slug)
        finally:
            reopened.close()

        repeated = httpx.post(
            f"{server_url}/api/network/ledger/found",
            json={"org": slug, "events": founded["wires"]},
            timeout=10,
        )
        assert repeated.status_code == 409
        assert _org_metadata(store_path) == metadata_before_founding

        tampered_slug = "tampered-founding"
        tampered_org_id = "019c0000-0000-7000-8000-000000000002"
        _create_org(orgs_dir, tampered_slug, tampered_org_id)
        tampered_path = org_ledger_db_path(tampered_slug)
        tampered_metadata = _org_metadata(tampered_path)
        monkeypatch.setenv("GRAPH_ORG", tampered_slug)
        tampered = _run_node(
            tmp_path,
            org=tampered_slug,
            org_id=tampered_org_id,
            root_seed_hex=root.private_hex,
            root_pub=root.public_hex,
            personal_root_seed_hex=PERSONAL_ROOT_SEED.hex(),
            now=NOW,
            server_url=None,
        )
        bad_event = dict(tampered["events"][2])
        bad_event["sig"] = (
            ("0" if bad_event["sig"][0] != "0" else "1")
            + bad_event["sig"][1:]
        )
        bad_wires = list(tampered["wires"])
        bad_wires[2] = canonical_json(bad_event).decode("ascii")
        refused = httpx.post(
            f"{server_url}/api/network/ledger/found",
            json={"org": tampered_slug, "events": bad_wires},
            timeout=10,
        )
        assert refused.status_code == 400
        assert _org_metadata(tampered_path) == tampered_metadata
        if tampered_path.exists():
            with LedgerStore(tampered_path) as untouched:
                assert len(untouched) == 0

    optional_slug = "optional-kem"
    optional_org_id = "019c0000-0000-7000-8000-000000000003"
    optional = _run_node(
        tmp_path,
        org=optional_slug,
        org_id=optional_org_id,
        root_seed_hex=root.private_hex,
        root_pub=root.public_hex,
        personal_root_seed_hex=PERSONAL_ROOT_SEED.hex(),
        kem_seed_hex=KEM_SEED.hex(),
        now=NOW,
        server_url=None,
    )
    credential = validate(optional["kemCredential"])
    with LedgerStore() as optional_reference_store:
        optional_reference = found_org_ledger(
            optional_reference_store,
            org_id=optional_org_id,
            org_root=root,
            personal_root_seed=PERSONAL_ROOT_SEED,
            now=NOW,
            kem_seed=KEM_SEED,
        )
        optional_reference_ids = [
            optional_reference.genesis_id,
            optional_reference.role_define_id,
            optional_reference.founding_invite_id,
            optional_reference.founder_claim_id,
        ]
        assert optional["eventIds"] == optional_reference_ids
        assert optional["events"] == [
            optional_reference_store.get(event_id).to_dict()
            for event_id in optional_reference_ids
        ]
        assert optional["kemCredential"] == optional_reference.kem_credential
        assert optional["kemPrivateKey"] == optional_reference.kem_private_key

    expected_private, expected_public = derive_encapsulation_keypair(
        KEM_SEED,
        kem_purpose(optional["genesisId"]),
    )
    assert optional["kemPrivateKey"] == expected_private
    assert credential.kem_public_key == expected_public
    assert credential.persona == optional["founderPersonaPub"]
    assert credential.genesis_id == optional["genesisId"]
