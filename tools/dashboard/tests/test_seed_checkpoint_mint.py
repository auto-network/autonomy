"""The seed checkpoint an existing org mints at sign-on, end to end (auto-tmers).

This is the production shape the unit fold fixtures could not exercise: the
local ledger is keyed by SLUG while the registry knows the org by its bound
UUID. ``checkpoint_due`` reads the slug's ledger DB but must stamp
``record["org"]`` with the uuid, because the registry — and the dashboard
forward route — reject a record whose org is not the path uuid. Here a real
registry subprocess adopts the engine-assembled, root-signed seed and then
reports it, proving the slug/uuid split is handled.
"""

from __future__ import annotations

import time

import httpx
import pytest

from tools.dashboard import membership_checkpoint as cp
from tools.network.idkit import KeyPair
from tools.network.idkit.persona import derive_persona
from tools.network.ledger import LedgerStore, org_ledger_db_path
from tools.network.ledger import membership_commitment as mc
from tools.network.ledger.found import found_org_ledger
from tools.network.registry.signing import sign_request
from tools.network.storagekit import credentials as cm

from .membership_sim._harness import Registry

SLUG = "acme-local"
UUID = "2d4b90cb-1e89-452b-82cb-68ca44fd8e52"
T0 = 1_800_000_000_000


@pytest.fixture
def orgs_dir(monkeypatch, tmp_path):
    # tools.graph.db pools connections in a module-level dict keyed by path,
    # and this fixture repoints the org/graph stores at a temp dir (through
    # checkpoint_due's cache read and record_adopted's write). A pooled handle
    # to this dir would survive teardown and be read by the next module in the
    # same xdist worker after the dir is gone. Drain the pool on both edges.
    from tools.graph.db import GraphDB
    d = tmp_path / "orgs"
    d.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(d))
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    GraphDB.close_all_pooled()
    try:
        yield d
    finally:
        GraphDB.close_all_pooled()


def _found():
    """Found the SLUG org's local ledger; return (root, founder persona, genesis)."""
    root = KeyPair.generate()
    seed = bytes(range(32))
    with LedgerStore(org_ledger_db_path(SLUG)) as store:
        res = found_org_ledger(
            store, org_id=SLUG, org_root=root, personal_root_seed=seed,
            now=T0, kem_seed=cm.derive_kem_seed(seed))
    persona = derive_persona(seed, res.genesis_id)
    return root, persona, res.genesis_id


def _sign_root_seed(record: dict, root: KeyPair) -> dict:
    """Sign the engine's unsigned seed with the org root, as the browser does:
    fill ``signer`` with the root pub, then sign the CHECKPOINT_DOMAIN input."""
    record["signer"] = root.public_hex
    record["sig"] = root.sign_hex(mc._signing_input(record))
    return record


def test_engine_seed_stamps_the_uuid_and_the_registry_adopts_it(orgs_dir, tmp_path):
    root, persona, genesis_id = _found()

    reg = Registry(tmp_path, org=UUID)
    try:
        # Register the org by its UUID (root-signed), as the real binding does.
        with httpx.Client(base_url=reg.http) as client:
            r = client.post("/v1/orgs", json=sign_request(
                root, "POST", "/v1/orgs",
                {"org_uuid": UUID, "root_pub": root.public_hex,
                 "recovery_policy": "none"},
                ts=int(time.time())))
            assert r.status_code == 201, r.text
        assert reg.membership_state() is None  # unseeded, exactly like autonomy

        # The engine assembles the seed from the SLUG's ledger, stamped with
        # the UUID — the fix under test.
        decision = cp.checkpoint_due(
            SLUG, persona.public_hex, ts=int(time.time()),
            genesis_id=genesis_id, org_uuid=UUID)
        assert decision.action == "assemble"
        assert decision.sign_with == cp.SIGN_WITH_ROOT
        assert decision.record["org"] == UUID
        assert decision.record["seq"] == 0
        assert decision.record["prev"] == genesis_id

        # Sign with the root and POST it; the registry adopts by induction.
        record = _sign_root_seed(decision.record, root)
        with httpx.Client(base_url=reg.http) as client:
            r = client.post(f"/v1/orgs/{UUID}/membership-checkpoints", json=record)
            assert r.status_code == 201, r.text

        state = reg.membership_state()
        assert state is not None
        assert state["seq"] == 0
        assert state["members_root"] == decision.record["members_root"]
        assert state["checkpointers_root"] == decision.record["checkpointers_root"]

        # Cached as adopted, the engine now reports up-to-date — no re-seed.
        cp.record_adopted(SLUG, record)
        again = cp.checkpoint_due(
            SLUG, persona.public_hex, ts=int(time.time()),
            genesis_id=genesis_id, org_uuid=UUID)
        assert again.action == "up-to-date"
    finally:
        reg.stop()


def test_uuid_defaults_to_slug_when_omitted(orgs_dir):
    """Back-compat: with no org_uuid the slug stands in (the unit-fixture
    behavior), so callers that coincide slug and uuid are unaffected."""
    _root, persona, genesis_id = _found()
    decision = cp.checkpoint_due(
        SLUG, persona.public_hex, ts=int(time.time()), genesis_id=genesis_id)
    assert decision.action == "assemble"
    assert decision.record["org"] == SLUG
