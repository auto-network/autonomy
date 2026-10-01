"""The one-time signing pass (auto-qrmlg.6 S4): every unsigned row of a
founded organization store is signed in place by the key its set declares,
through the same boundary a fresh write passes; nothing else is touched."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tools.dashboard import settings_signing_pass as pass_module
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import SettingSchema, field, home, signer
from tools.network.fleet_sync.materialize import LEDGER_EVENT_SET_ID
from tools.network.idkit import KeyPair
from tools.network.ledger.projections import organization_content_domain_id
from tools.network.ledger.scopes import settings_sign_scope
from tools.network.ledger.store import LedgerStore, org_ledger_db_path
from tools.network.ledger.tests.conftest import Sim
from tools.network.settingskit import authority
from tools.network.settingskit.envelope import record_from_row, verify_record
from tools.network.storagekit import storage_delegate_scopes

ORG = "anchore"
ORG_UUID = "11111111-1111-4111-8111-111111111111"
SET_ID = "test.pass.plain"
ATTENDED_SET_ID = "test.pass.attended"


@home("organization")
class _PlainV1(SettingSchema):
    set_id = SET_ID
    schema_revision = 1
    label: str = field(required=True, description="a label")


@home("organization")
@signer("persona")
class _AttendedV1(SettingSchema):
    set_id = ATTENDED_SET_ID
    schema_revision = 1
    label: str = field(required=True, description="a label")


@pytest.fixture
def founded(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal", path=orgs_dir / "personal.db").close()
    GraphDB.create_org_db(ORG, root=orgs_dir, org_id=ORG_UUID).close()
    sim = Sim(org=ORG_UUID)
    scopes = storage_delegate_scopes(organization_content_domain_id(sim.genesis_id))
    sim.role_define(sim.root, "member", scope_set=scopes, requires="self")
    member = KeyPair.generate()
    sim.claim(sim.invite(sim.root, "member", invite_key=member), member, member)
    delegate = KeyPair.generate()
    sim.delegate(member, delegate, scopes, ttl=60_000)
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        store.append_bundle(list(sim.ledger.events()))
    monkeypatch.setattr(settings_ops, "_SIGNER_PROVIDER", None)
    monkeypatch.setattr(settings_ops, "_UNSIGNED_WARNED", set())
    authority.forget()
    # The pre-S2 population: written with NO signer installed.
    settings_ops.install_signer_provider(lambda org: None)
    base = settings_ops.upsert_by_key(SET_ID, 1, "a", {"label": "a"}, org=ORG)
    settings_ops.override_setting(base, {"label": "a-patched"}, org=ORG)
    settings_ops.upsert_by_key(SET_ID, 1, "b", {"label": "b"}, org=ORG)
    settings_ops.add_setting(SET_ID, 1, "c", {"label": "c"}, org=ORG)
    settings_ops.add_setting(ATTENDED_SET_ID, 1, "att", {"label": "attended"}, org=ORG)
    context = settings_ops.SigningContext(
        key=delegate, terminal_persona=member.public_hex, genesis_id=sim.genesis_id,
    )
    settings_ops.install_signer_provider(lambda org: context if org == ORG else None)
    yield {"sim": sim, "member": member, "delegate": delegate, "genesis": sim.genesis_id}
    authority.forget()
    GraphDB.close_all_pooled()


def _rows(where="1=1"):
    conn = sqlite3.connect(org_ledger_db_path(ORG))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM settings WHERE {where} ORDER BY rowid")]
    finally:
        conn.close()


def test_a_dry_run_reports_and_writes_nothing(founded):
    report = pass_module.sign_org_store(ORG, apply=False)
    assert report["founded"] and report["unsigned"] == 5   # 4 plain rows + 1 attended
    assert report["signed"] == 4 and report["persona_tier"] == 1 and report["refused"] == {}
    assert all(r["signature"] is None for r in _rows())


def test_every_delegate_signer_row_is_signed_in_place_and_verifies(founded):
    before = {r["id"]: r for r in _rows()}
    report = pass_module.sign_org_store(ORG, apply=True, chunk_rows=2)
    assert report["signed"] == 4 and report["persona_tier"] == 1 and report["transactions"] == 2
    after = {r["id"]: r for r in _rows()}
    assert set(after) == set(before)   # in place: no row created or removed
    for row_id, row in after.items():
        if row["set_id"] == LEDGER_EVENT_SET_ID or row["set_id"] == ATTENDED_SET_ID:
            assert row["signature"] is None, row["set_id"]
            continue
        assert row["signing_key"] == founded["delegate"].public_hex
        assert row["terminal_persona"] == founded["member"].public_hex
        assert row["payload"] == before[row_id]["payload"] and row["supersedes"] == before[row_id]["supersedes"]
        verify_record(record_from_row(row, founded["genesis"]), row["signature"])
    # Idempotent: a second run finds nothing but the persona-tier row.
    again = pass_module.sign_org_store(ORG, apply=True)
    assert again["signed"] == 0 and again["unsigned"] == 1 and again["persona_tier"] == 1


def test_without_a_signer_nothing_is_written(founded):
    settings_ops.install_signer_provider(lambda org: None)
    report = pass_module.sign_org_store(ORG, apply=True)
    assert report["no_signer"] is True and report["signed"] == 0 and report["transactions"] == 0
    assert all(r["signature"] is None for r in _rows())


def test_a_row_the_boundary_refuses_is_left_unsigned_and_counted(founded):
    sim = founded["sim"]
    scopes = storage_delegate_scopes(organization_content_domain_id(sim.genesis_id))
    sim.role_define(sim.root, "editor", scope_set=list(scopes) + [settings_sign_scope("some.other.set")], requires="self")
    narrowed = KeyPair.generate()
    sim.claim(sim.invite(sim.root, "editor", invite_key=narrowed), narrowed, narrowed)
    delegate = KeyPair.generate()
    sim.delegate(narrowed, delegate, scopes, ttl=60_000)
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        store.append_bundle(list(sim.ledger.events()))
    context = settings_ops.SigningContext(key=delegate, terminal_persona=narrowed.public_hex, genesis_id=sim.genesis_id)
    settings_ops.install_signer_provider(lambda org: context if org == ORG else None)
    report = pass_module.sign_org_store(ORG, apply=True)
    assert report["signed"] == 0 and report["refused"] == {"signer_lacks_settings_sign": 4}
    assert all(r["signature"] is None for r in _rows())


def test_an_unfounded_store_and_the_run_wrapper(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal", path=orgs_dir / "personal.db").close()
    GraphDB.create_org_db(ORG, root=orgs_dir, org_id=ORG_UUID).close()
    monkeypatch.setattr(settings_ops, "_SIGNER_PROVIDER", None)
    report = pass_module.sign_org_store(ORG, apply=True)
    assert report["founded"] is False and report["signed"] == 0
    monkeypatch.setattr(pass_module, "org_slugs", lambda: [ORG, "missing-org"])
    reports = pass_module.run(apply=False)
    assert [r["org"] for r in reports] == [ORG, "missing-org"]
    # A store that is not there is reported not founded, never an error.
    assert reports[0]["founded"] is False and reports[1]["founded"] is False
    GraphDB.close_all_pooled()


def test_the_unlock_hook_runs_only_on_the_singular_ownership_machine(monkeypatch):
    from types import SimpleNamespace
    from tools.network import fleet_tunnel_server

    calls = []
    monkeypatch.setattr(pass_module, "run", lambda apply=True, orgs=None, pause_s=0.05: calls.append(apply) or [])
    monkeypatch.setattr(fleet_tunnel_server, "state", lambda: SimpleNamespace(allowed=True, reason=None))
    # Off by default: a worker respawn re-warms the vault through the same
    # seam as an unlock and must not start the pass unasked.
    monkeypatch.setattr(pass_module, "RUN_AFTER_UNLOCK", False)
    pass_module.run_after_unlock()
    assert calls == []
    monkeypatch.setattr(pass_module, "RUN_AFTER_UNLOCK", True)
    monkeypatch.setattr(fleet_tunnel_server, "state", lambda: SimpleNamespace(allowed=False, reason="not-elected"))
    pass_module.run_after_unlock()
    assert calls == []
    monkeypatch.setattr(fleet_tunnel_server, "state", lambda: SimpleNamespace(allowed=True, reason=None))
    pass_module.run_after_unlock()
    assert calls == [True]


def test_the_pass_does_not_hold_up_a_concurrent_reader_on_the_pooled_connection(founded, monkeypatch):
    """Live 2026-09-29 19:10-19:16Z: the pass ran on the dashboard's pooled
    connection and every reader of the store, the event loop included,
    queued behind it. The pass now opens its own connection; a reader on
    the pooled handle answers while it runs."""
    import threading
    import time

    # A bigger UNSIGNED population (written with no signer installed), so
    # the pass takes long enough to overlap the reader.
    signer = settings_ops._SIGNER_PROVIDER
    settings_ops.install_signer_provider(lambda org: None)
    for i in range(800):
        settings_ops.add_setting(SET_ID, 1, f"bulk{i}", {"label": str(i)}, org=ORG)
    settings_ops.install_signer_provider(signer)
    monkeypatch.setattr(pass_module, "CHUNK_ROWS", 50)
    reads: list[float] = []
    stop = threading.Event()

    def reader():
        pooled = settings_ops._open(ORG)
        while not stop.is_set():
            started = time.perf_counter()
            pooled.conn.execute("SELECT count(*) FROM settings WHERE set_id=?", (SET_ID,)).fetchone()
            reads.append(time.perf_counter() - started)
            time.sleep(0.002)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        report = pass_module.sign_org_store(ORG, apply=True, chunk_rows=50, pause_s=0.001)
    finally:
        stop.set()
        thread.join(timeout=5)
    # 800 rows in 50-row chunks: the same 16 commits for the reader to
    # overlap as 1500 in 100s, at half the rows to write and sign.
    assert report["signed"] >= 800 and report["transactions"] >= 16
    assert reads, "the reader never ran"
    # No single read waited on the pass for more than a chunk's write.
    assert max(reads) < 0.25, f"slowest concurrent read {max(reads):.3f}s over {len(reads)} reads"
