"""Organization settings rows are written signed (auto-qrmlg.6 S2), and
only by a signer the organization's fold authorizes (S3, the boundary at
the write).

A row written into a FOUNDED organization store by a process that holds a
signing context for that organization carries the envelope: the addressed
record signed by the delegate key, the delegate's member persona as the
terminal persona. Personal and machine rows, unfounded organization stores,
and a process with no signer for the organization keep writing unsigned
rows, exactly as before. A signer the fold refuses — unknown, revoked, a
role narrowed away from the set, not the row persona on a persona-keyed
set, or a delegate on a persona-tier set — writes nothing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import SettingSchema, field, home, keyed_per_entity, signer
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
SET_ID = "test.signing.plain"
PERSONA_SET_ID = "test.signing.by-persona"
ATTENDED_SET_ID = "test.signing.attended"


@home("organization")
class _PlainOrgV1(SettingSchema):
    """A plain organization-homed set for the signing tests."""

    set_id = SET_ID
    schema_revision = 1
    label: str = field(required=True, description="a label")


@home("organization")
@keyed_per_entity(key_strategy="member_public_key")
class _ByPersonaV1(SettingSchema):
    """An organization set keyed by the member persona."""

    set_id = PERSONA_SET_ID
    schema_revision = 1
    label: str = field(required=True, description="a label")


@home("organization")
@signer("persona")
class _AttendedV1(SettingSchema):
    """An organization set whose rows only the persona may sign (D8)."""

    set_id = ATTENDED_SET_ID
    schema_revision = 1
    label: str = field(required=True, description="a label")


@dataclass
class Founded:
    sim: Sim
    genesis_id: str
    member: KeyPair      # a member whose role holds the storage scopes
    delegate: KeyPair    # that member's storage delegate (the process key)

    def storage_member(self, role="member", scope_set=None):
        """Another member of *role* with a storage delegate; the events are
        appended to the store. Returns (member, delegate)."""
        sim = self.sim
        scopes = storage_delegate_scopes(organization_content_domain_id(sim.genesis_id))
        if role not in sim.fold().role_defs:
            sim.role_define(sim.root, role, scope_set=scopes if scope_set is None else scope_set, requires="self")
        member = KeyPair.generate()
        sim.claim(sim.invite(sim.root, role, invite_key=member), member, member)
        delegate = KeyPair.generate()
        grant = sim.delegate(member, delegate, scopes, ttl=60_000)
        assert sim.fold().valid[grant] is True, sim.fold().reasons.get(grant)
        self.sync()
        return member, delegate

    def sync(self):
        """Append every simulator event the store does not hold yet."""
        with LedgerStore(org_ledger_db_path(ORG)) as store:
            store.append_bundle(list(self.sim.ledger.events()))


@pytest.fixture
def founded_org(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal", path=orgs_dir / "personal.db").close()
    GraphDB.create_org_db(ORG, root=orgs_dir, org_id=ORG_UUID).close()
    founded = Founded(Sim(org=ORG_UUID), "", KeyPair.generate(), KeyPair.generate())
    founded.genesis_id = founded.sim.genesis_id
    founded.member, founded.delegate = founded.storage_member()
    monkeypatch.setattr(settings_ops, "_SIGNER_PROVIDER", None)
    monkeypatch.setattr(settings_ops, "_UNSIGNED_WARNED", set())
    authority.forget()
    yield founded
    authority.forget()
    GraphDB.close_all_pooled()


def _rows(org, set_id=SET_ID):
    conn = sqlite3.connect(org_ledger_db_path(org))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM settings WHERE set_id=? ORDER BY created_at, rowid", (set_id,))]
    finally:
        conn.close()


def _provider_for(founded, key: KeyPair = None, persona: str = None):
    from tools.graph.settings_ops import SigningContext

    key = key or founded.delegate
    persona = persona or founded.member.public_hex

    def provider(org):
        return SigningContext(key=key, terminal_persona=persona, genesis_id=founded.genesis_id, witness=None) \
            if org == ORG else None
    return provider


def test_an_org_row_is_signed_over_its_addressed_record(founded_org):
    key, persona = founded_org.delegate, founded_org.member.public_hex
    settings_ops.install_signer_provider(_provider_for(founded_org))
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
    key, persona = founded_org.delegate, founded_org.member.public_hex
    settings_ops.install_signer_provider(_provider_for(founded_org))
    base = settings_ops.upsert_by_key(SET_ID, 1, "k2", {"label": "base"}, org=ORG)
    first = settings_ops.override_setting(base, {"label": "patched"}, org=ORG)
    excluded = settings_ops.exclude_setting(base, org=ORG) if hasattr(settings_ops, "exclude_setting") else None
    rows = {r["id"]: r for r in _rows(ORG)}
    for row in rows.values():
        assert row["signing_key"] == key.public_hex and row["terminal_persona"] == persona, row["key"]
        verify_record(record_from_row(row, founded_org.genesis_id), row["signature"])
    assert first in rows
    if excluded is not None:
        assert rows[excluded]["excludes"] == base
    # An upsert over an existing signed base row re-signs it over the new payload.
    settings_ops.upsert_by_key(SET_ID, 1, "k2", {"label": "base-2"}, org=ORG)
    again = {r["id"]: r for r in _rows(ORG)}[base]
    assert json.loads(again["payload"]) == {"label": "base-2"} and again["signed_at"] >= rows[base]["signed_at"]
    verify_record(record_from_row(again, founded_org.genesis_id), again["signature"])


def test_a_row_signed_by_another_signer_is_never_rewritten(founded_org):
    ours, our_persona = founded_org.delegate, founded_org.member.public_hex
    their_member, theirs = founded_org.storage_member()
    settings_ops.install_signer_provider(_provider_for(founded_org, theirs, their_member.public_hex))
    theirs_id = settings_ops.upsert_by_key(SET_ID, 1, "shared", {"label": "theirs"}, org=ORG)
    settings_ops.install_signer_provider(_provider_for(founded_org))
    ours_id = settings_ops.upsert_by_key(SET_ID, 1, "shared", {"label": "ours"}, org=ORG)
    assert ours_id != theirs_id
    rows = {r["id"]: r for r in _rows(ORG)}
    assert json.loads(rows[theirs_id]["payload"]) == {"label": "theirs"} and rows[theirs_id]["signing_key"] == theirs.public_hex
    assert rows[ours_id]["signing_key"] == ours.public_hex and rows[ours_id]["terminal_persona"] == our_persona
    for row in rows.values():
        verify_record(record_from_row(row, founded_org.genesis_id), row["signature"])


def test_personal_and_machine_rows_stay_unsigned(founded_org):
    settings_ops.install_signer_provider(_provider_for(founded_org))
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


def test_a_signer_the_fold_does_not_authorize_writes_nothing(founded_org):
    stranger = KeyPair.generate()
    settings_ops.install_signer_provider(_provider_for(founded_org, stranger, "ss" * 32))
    with pytest.raises(settings_ops.SettingsSignerRefused) as refused:
        settings_ops.add_setting(SET_ID, 1, "k6", {"label": "x"}, org=ORG)
    assert refused.value.reason == "signer_unknown"
    assert _rows(ORG) == []
    # A revoked delegate: still attributable, refused for the present.
    founded_org.sim.revoke_key(founded_org.sim.root, founded_org.delegate)
    founded_org.sync()
    settings_ops.install_signer_provider(_provider_for(founded_org))
    with pytest.raises(settings_ops.SettingsSignerRefused) as refused:
        settings_ops.upsert_by_key(SET_ID, 1, "k6", {"label": "x"}, org=ORG)
    # A revoked delegation drops out of the fold's delegation view, so the
    # key is refused as unknown or as revoked (settingskit.boundary S1
    # accepts both); either way nothing is written.
    assert refused.value.reason in ("signer_key_revoked", "signer_unknown")
    assert _rows(ORG) == []


def test_a_narrowed_role_is_refused_for_the_sets_it_does_not_name(founded_org):
    scopes = storage_delegate_scopes(organization_content_domain_id(founded_org.genesis_id))
    member, delegate = founded_org.storage_member(
        "editor", scope_set=list(scopes) + [settings_sign_scope(PERSONA_SET_ID)],
    )
    settings_ops.install_signer_provider(_provider_for(founded_org, delegate, member.public_hex))
    with pytest.raises(settings_ops.SettingsSignerRefused) as refused:
        settings_ops.add_setting(SET_ID, 1, "k7", {"label": "x"}, org=ORG)
    assert refused.value.reason == "signer_lacks_settings_sign"
    assert _rows(ORG) == []


def test_a_persona_keyed_set_takes_only_the_row_persona(founded_org):
    settings_ops.install_signer_provider(_provider_for(founded_org))
    other, _ = founded_org.storage_member()
    with pytest.raises(settings_ops.SettingsSignerRefused) as refused:
        settings_ops.add_setting(PERSONA_SET_ID, 1, other.public_hex, {"label": "theirs"}, org=ORG)
    assert refused.value.reason == "signer_is_not_row_persona"
    sid = settings_ops.add_setting(PERSONA_SET_ID, 1, founded_org.member.public_hex, {"label": "mine"}, org=ORG)
    [row] = _rows(ORG, PERSONA_SET_ID)
    assert row["id"] == sid and row["terminal_persona"] == founded_org.member.public_hex
    verify_record(record_from_row(row, founded_org.genesis_id), row["signature"])


def test_a_persona_tier_set_is_not_signed_by_the_delegate(founded_org):
    settings_ops.install_signer_provider(_provider_for(founded_org))
    with pytest.raises(settings_ops.SettingsSignerRefused) as refused:
        settings_ops.add_setting(ATTENDED_SET_ID, 1, "k8", {"label": "x"}, org=ORG)
    assert refused.value.reason == "signer_tier_persona_required"
    assert _rows(ORG, ATTENDED_SET_ID) == []


def test_the_write_boundary_reads_the_fold_once_per_ledger_depth(founded_org, monkeypatch):
    settings_ops.install_signer_provider(_provider_for(founded_org))
    calls = []
    real = authority.store_fold.__wrapped__ if hasattr(authority.store_fold, "__wrapped__") else authority.store_fold

    def counting(conn):
        calls.append(authority.ledger_depth(conn))
        return real(conn)

    monkeypatch.setattr(authority, "store_fold", counting)
    for i in range(5):
        settings_ops.add_setting(SET_ID, 1, f"k{i}", {"label": str(i)}, org=ORG)
    # Five writes, one depth: the fold was consulted each time (the depth
    # query) and rebuilt none of those times beyond the first.
    assert len(calls) == 5 and len(set(calls)) == 1
    assert len(_rows(ORG)) == 5


def test_the_signer_is_prepared_before_the_write_lock(founded_org, monkeypatch):
    """The envelope is built, and its signer's fold read, before BEGIN
    IMMEDIATE: the write lock is held for the write alone, never through
    the signing. A signature is taken once and written as is."""
    settings_ops.install_signer_provider(_provider_for(founded_org))
    in_transaction = []
    real = settings_ops.prepare_signer

    def recording(db, org):
        in_transaction.append(db.conn.in_transaction)
        return real(db, org)

    monkeypatch.setattr(settings_ops, "prepare_signer", recording)
    base = settings_ops.upsert_by_key(SET_ID, 1, "k9", {"label": "new"}, org=ORG)
    settings_ops.upsert_by_key(SET_ID, 1, "k9", {"label": "over an existing row"}, org=ORG)
    settings_ops.override_setting(base, {"label": "layer"}, org=ORG)
    assert in_transaction and not any(in_transaction), in_transaction
    for row in _rows(ORG):
        verify_record(record_from_row(row, founded_org.genesis_id), row["signature"])


def test_the_write_boundary_folds_with_the_current_time(founded_org, monkeypatch):
    """Grant expiry is judged against the fold's clock; a fold with no clock
    never expires a delegation. The boundary passes the current time."""
    import tools.network.ledger as ledger_mod
    from tools.network.clock import now_ms

    settings_ops.install_signer_provider(_provider_for(founded_org))
    seen = []
    real_fold = ledger_mod.fold

    def spy(ledger, heads=None, now=None):
        seen.append(now)
        return real_fold(ledger, heads, now)

    monkeypatch.setattr(ledger_mod, "fold", spy)
    authority._FOLD_CACHE.clear()
    settings_ops.add_setting(SET_ID, 1, "k-now", {"label": "x"}, org=ORG)
    assert seen, "the boundary folded nothing"
    assert all(isinstance(n, int) and abs(n - now_ms()) < 60_000 for n in seen)
