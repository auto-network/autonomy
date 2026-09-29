"""Organization settings rows are written signed (auto-qrmlg.6 S2).

A row written into a FOUNDED organization store by a process that holds a
signing context for that organization carries the envelope: the addressed
record signed by the delegate key, the delegate's member persona as the
terminal persona. Personal and machine rows, unfounded organization stores,
and a process with no signer for the organization keep writing unsigned
rows, exactly as before.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import SettingSchema, field, home
from tools.network.idkit import KeyPair
from tools.network.ledger.found import found_org_ledger
from tools.network.ledger.store import LedgerStore, org_ledger_db_path
from tools.network.settingskit.envelope import record_from_row, verify_record

ORG = "anchore"
ORG_UUID = "11111111-1111-4111-8111-111111111111"
SET_ID = "test.signing.plain"


@home("organization")
class _PlainOrgV1(SettingSchema):
    """A plain organization-homed set for the signing tests."""

    set_id = SET_ID
    schema_revision = 1
    label: str = field(required=True, description="a label")


@pytest.fixture
def founded_org(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal", path=orgs_dir / "personal.db").close()
    GraphDB.create_org_db(ORG, root=orgs_dir, org_id=ORG_UUID).close()
    root, personal_root = KeyPair.generate(), KeyPair.generate()
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        founded = found_org_ledger(
            store, org_id=ORG_UUID, org_root=root,
            personal_root_seed=bytes.fromhex(personal_root.private_hex), now=int(time.time() * 1000),
        )
    monkeypatch.setattr(settings_ops, "_SIGNER_PROVIDER", None)
    monkeypatch.setattr(settings_ops, "_UNSIGNED_WARNED", set())
    yield founded
    GraphDB.close_all_pooled()


def _rows(org, set_id=SET_ID):
    conn = sqlite3.connect(org_ledger_db_path(org))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM settings WHERE set_id=? ORDER BY created_at, rowid", (set_id,))]
    finally:
        conn.close()


def _provider_for(founded, key: KeyPair, persona: str):
    from tools.graph.settings_ops import SigningContext

    def provider(org):
        return SigningContext(key=key, terminal_persona=persona, genesis_id=founded.genesis_id, witness=None) \
            if org == ORG else None
    return provider


def test_an_org_row_is_signed_over_its_addressed_record(founded_org):
    key, persona = KeyPair.generate(), "pp" * 32
    settings_ops.install_signer_provider(_provider_for(founded_org, key, persona))
    sid = settings_ops.add_setting(SET_ID, 1, "k1", {"label": "one"}, org=ORG)
    [row] = _rows(ORG)
    assert row["id"] == sid and row["signing_key"] == key.public_hex and row["terminal_persona"] == persona
    assert row["witness"] is None and isinstance(row["signed_at"], int) and row["signature"]
    # The stored row rebuilds the exact record the signature covers.
    verify_record(record_from_row(row, founded_org.genesis_id), row["signature"])
    # ... and a column edited after signing breaks it.
    tampered = dict(row, payload=json.dumps({"label": "two"}))
    with pytest.raises(Exception):
        verify_record(record_from_row(tampered, founded_org.genesis_id), row["signature"])


def test_every_write_path_signs(founded_org):
    key, persona = KeyPair.generate(), "pp" * 32
    settings_ops.install_signer_provider(_provider_for(founded_org, key, persona))
    base = settings_ops.upsert_by_key(SET_ID, 1, "k2", {"label": "base"}, org=ORG)
    first = settings_ops.override_setting(base, {"label": "patched"}, org=ORG)
    second = settings_ops.override_setting(base, {"label": "patched again"}, org=ORG, deprecate_previous=True)
    excluded = settings_ops.exclude_setting(base, org=ORG) if hasattr(settings_ops, "exclude_setting") else None
    rows = {r["id"]: r for r in _rows(ORG)}
    for row in rows.values():
        assert row["signing_key"] == key.public_hex and row["terminal_persona"] == persona, row["key"]
        verify_record(record_from_row(row, founded_org.genesis_id), row["signature"])
    assert rows[first]["supersedes"] == base and rows[second]["supersedes"] == base
    # The collapsed layer was re-signed AS deprecated, naming its successor.
    assert rows[first]["deprecated"] == 1 and rows[first]["successor_id"] == second
    assert rows[second]["deprecated"] == 0
    if excluded is not None:
        assert rows[excluded]["excludes"] == base
    # An upsert over an existing signed base row re-signs it over the new payload.
    settings_ops.upsert_by_key(SET_ID, 1, "k2", {"label": "base-2"}, org=ORG)
    again = {r["id"]: r for r in _rows(ORG)}[base]
    assert json.loads(again["payload"]) == {"label": "base-2"} and again["signed_at"] >= rows[base]["signed_at"]
    verify_record(record_from_row(again, founded_org.genesis_id), again["signature"])


def test_a_row_signed_by_another_signer_is_never_rewritten(founded_org):
    ours, theirs = KeyPair.generate(), KeyPair.generate()
    settings_ops.install_signer_provider(_provider_for(founded_org, theirs, "tt" * 32))
    theirs_id = settings_ops.upsert_by_key(SET_ID, 1, "shared", {"label": "theirs"}, org=ORG)
    settings_ops.install_signer_provider(_provider_for(founded_org, ours, "oo" * 32))
    ours_id = settings_ops.upsert_by_key(SET_ID, 1, "shared", {"label": "ours"}, org=ORG)
    assert ours_id != theirs_id
    rows = {r["id"]: r for r in _rows(ORG)}
    assert json.loads(rows[theirs_id]["payload"]) == {"label": "theirs"} and rows[theirs_id]["signing_key"] == theirs.public_hex
    assert rows[ours_id]["signing_key"] == ours.public_hex and rows[ours_id]["terminal_persona"] == "oo" * 32
    for row in rows.values():
        verify_record(record_from_row(row, founded_org.genesis_id), row["signature"])


def test_personal_and_machine_rows_stay_unsigned(founded_org):
    key, persona = KeyPair.generate(), "pp" * 32
    settings_ops.install_signer_provider(_provider_for(founded_org, key, persona))
    from tools.graph.schemas.registry import SettingSchema as _S, field as _f, home as _h

    @_h("personal")
    class _PersonalV1(_S):
        set_id = "test.signing.personal"
        schema_revision = 1
        label: str = _f(required=True, description="a label")

    settings_ops.add_setting("test.signing.personal", 1, "p", {"label": "mine"}, org="personal")
    [row] = _rows("personal", "test.signing.personal")
    assert row["signing_key"] is None and row["signature"] is None and row["terminal_persona"] is None


def test_without_a_signer_the_row_is_unsigned_and_the_gap_is_said_once(founded_org, caplog):
    import logging

    settings_ops.install_signer_provider(lambda org: None)
    with caplog.at_level(logging.WARNING, logger="tools.graph.settings_ops"):
        settings_ops.add_setting(SET_ID, 1, "k3", {"label": "x"}, org=ORG)
        settings_ops.add_setting(SET_ID, 1, "k4", {"label": "y"}, org=ORG)
    rows = _rows(ORG)
    assert len(rows) == 2 and all(r["signature"] is None for r in rows)
    warned = [r for r in caplog.records if "no storage delegate is held" in r.getMessage()]
    assert len(warned) == 1 and "'anchore'" in warned[0].getMessage()


def test_an_unfounded_org_store_stays_unsigned(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal", path=orgs_dir / "personal.db").close()
    GraphDB.create_org_db(ORG, root=orgs_dir, org_id=ORG_UUID).close()   # no ledger founded
    monkeypatch.setattr(settings_ops, "_SIGNER_PROVIDER", None)
    key = KeyPair.generate()

    from tools.graph.settings_ops import SigningContext
    settings_ops.install_signer_provider(
        lambda org: SigningContext(key=key, terminal_persona="pp" * 32, genesis_id="", witness=None))
    settings_ops.add_setting(SET_ID, 1, "k5", {"label": "z"}, org=ORG)
    [row] = _rows(ORG)
    assert row["signature"] is None
    GraphDB.close_all_pooled()
