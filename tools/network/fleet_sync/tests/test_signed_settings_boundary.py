"""The signed-settings boundary at sync apply (auto-qrmlg.6 S3).

A signed settings row arriving for a founded organization store passes
steps 2 to 6 of the design of record against the STORE'S OWN fold, read
from the ledger-event rows in the same database: the key resolves to a
current member entitled to the set (or, for a persona-keyed set, to the row
persona), its stored terminal persona is that persona, and its ``signed_at``
is newer than the same persona's row already in the slot. A refusal parks
the row with its reason; the fold-dependent ones are re-judged by the drain
so a lagging store converges with a current one.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync import materialize as materialize_module
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.materialize import LEDGER_EVENT_SET_ID
from tools.network.idkit import KeyPair
from tools.network.ledger.projections import organization_content_domain_id
from tools.network.ledger.scopes import settings_sign_scope
from tools.network.ledger.tests.conftest import Sim
from tools.network.settingskit import authority
from tools.network.settingskit.envelope import build_record, sign_record
from tools.network.storagekit import storage_delegate_scopes

SET_ID = "dashboard.example"
OTHER_SET = "dashboard.other"
PROFILE_SET = "autonomy.org.member-profile"   # keyed by the member persona
KEY = "shared-key"


def _member(sim: Sim, role: str, scope_set=None):
    """A claimed member of *role*. A new role holds the storage delegate
    scopes (so its members may mint a storage delegate) unless *scope_set*
    narrows it."""
    if role not in sim.fold().role_defs:
        if scope_set is None:
            scope_set = storage_delegate_scopes(organization_content_domain_id(sim.genesis_id))
        sim.role_define(sim.root, role, scope_set=scope_set, requires="self")
    persona = KeyPair.generate()
    sim.claim(sim.invite(sim.root, role, invite_key=persona), persona, persona)
    return persona


def _delegate(sim: Sim, member: KeyPair) -> KeyPair:
    scopes = storage_delegate_scopes(organization_content_domain_id(sim.genesis_id))
    child = KeyPair.generate()
    grant = sim.delegate(member, child, scopes, ttl=60_000)
    assert sim.fold().valid[grant] is True, sim.fold().reasons.get(grant)
    return child


def _open(path: Path, origin: str) -> tuple[GraphDB, MutationCatalog]:
    db = GraphDB(path)
    catalog = MutationCatalog(db.conn, origin)
    catalog.install()
    return db, catalog


def _event_rows(sim: Sim, events=None):
    for event in (events if events is not None else sim.ledger.events()):
        yield (str(uuid.uuid4()), LEDGER_EVENT_SET_ID, 1, event.event_id,
               json.dumps({"wire": event.to_json().decode("utf-8")}), "published")


def _write_ledger(db, catalog, ts: int, sim: Sim, events=None, transaction=None) -> None:
    rows = list(_event_rows(sim, events))
    with catalog.transaction(ts, transaction or f"ledger-{ts}"):
        for row in rows:
            db.conn.execute(
                "INSERT INTO settings (id,set_id,schema_revision,key,payload,publication_state)"
                " VALUES (?,?,?,?,?,?)", row,
            )


def _signed_row(genesis: str, signer: KeyPair, persona: str, payload: dict, signed_at: int,
                *, set_id=SET_ID, key=KEY, row_id=None):
    record = build_record(
        org=genesis, set_id=set_id, key=key, schema_revision=1,
        publication_state="published", deprecated=False, successor_id=None,
        payload=payload, signed_at=signed_at, signing_key=signer.public_hex, witness=None,
    )
    return (row_id or str(uuid.uuid4()), set_id, 1, key, json.dumps(payload), "published", 0, None,
            signed_at, signer.public_hex, sign_record(signer, record), None, persona)


def _write_signed(db, catalog, ts: int, row: tuple, transaction=None) -> str:
    with catalog.transaction(ts, transaction or f"signed-{ts}"):
        db.conn.execute(
            "INSERT INTO settings (id,set_id,schema_revision,key,payload,"
            "publication_state,deprecated,successor_id,signed_at,signing_key,"
            "signature,witness,terminal_persona) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row,
        )
    return row[0]


def _exchange(src: MutationCatalog, src_origin: str, dst: MutationCatalog) -> None:
    held = dst.origin_watermarks().get(src_origin, 0)
    position: tuple[int, str | None] = (held, None)
    while True:
        page = src.next_transactions_for_origin(src_origin, position[0], position[1], limit=50)
        if not page:
            return
        for _ref, timestamp, transaction_id, items in page:
            dst.apply_remote_batch(items)
            position = (timestamp, transaction_id)


def _rows(db, set_id=SET_ID, key=KEY):
    return [
        (r[0], json.loads(r[1])["v"])
        for r in db.conn.execute(
            "SELECT terminal_persona,payload FROM settings WHERE set_id=? AND key=?"
            " ORDER BY terminal_persona", (set_id, key),
        )
    ]


def _quarantine(db):
    import sqlite3
    try:
        rows = db.conn.execute("SELECT reason,retries FROM fleet_sync_quarantine").fetchall()
    except sqlite3.OperationalError:   # never created: nothing was ever parked
        return []
    return sorted((r[0], int(r[1])) for r in rows)


def _forwarded(db, catalog, origin: str, transaction: str) -> int:
    ref, = [r[0] for r in db.conn.execute(
        "SELECT t.id FROM fleet_sync_transactions t JOIN fleet_sync_origins o"
        " ON o.id=t.origin_id WHERE o.incarnation=? AND t.transaction_id=?",
        (origin, transaction),
    )]
    return len(catalog.transaction_items(ref, origin, transaction))


@pytest.fixture(autouse=True)
def _fresh_fold_cache():
    authority.forget()
    yield
    authority.forget()


@pytest.fixture
def pair(tmp_path):
    a_db, a = _open(tmp_path / "a.db", "a" * 64)
    b_db, b = _open(tmp_path / "b.db", "b" * 64)
    try:
        yield a_db, a, b_db, b
    finally:
        a_db.close()
        b_db.close()


def test_a_members_delegate_lands_with_the_member_as_the_slot(pair):
    a_db, a, b_db, b = pair
    sim = Sim()
    member = _member(sim, "member")
    delegate = _delegate(sim, member)
    _write_ledger(a_db, a, 10, sim)
    _write_ledger(b_db, b, 11, sim)
    _write_signed(a_db, a, 100, _signed_row(sim.genesis_id, delegate, member.public_hex, {"v": "one"}, 1_000))
    _exchange(a, "a" * 64, b)
    assert _rows(b_db) == [(member.public_hex, "one")]
    assert _quarantine(b_db) == []


def test_a_stranger_is_parked_forwarded_and_lands_once_its_claim_arrives(pair):
    a_db, a, b_db, b = pair
    sim = Sim()
    _member(sim, "member")
    _write_ledger(a_db, a, 10, sim)
    _write_ledger(b_db, b, 11, sim)
    known = list(sim.ledger.events())
    # A signs with a key the ledger does not know yet ...
    stranger = _member(sim, "member")
    _write_signed(a_db, a, 100, _signed_row(sim.genesis_id, stranger, stranger.public_hex, {"v": "early"}, 1_000))
    _exchange(a, "a" * 64, b)
    assert _rows(b_db) == []
    assert _quarantine(b_db) == [("settings_signer_unknown", 0)]
    # ... the parked row travels on with its signature ...
    assert _forwarded(b_db, b, "a" * 64, "signed-100") == 1
    # ... a drain before the claim re-parks it, counting the retry ...
    assert b.drain_pending_signatures() == 0
    assert _quarantine(b_db) == [("settings_signer_unknown", 1)]
    # ... and once the claim arrives the drain lands it.
    later = [e for e in sim.ledger.events() if e not in known]
    _write_ledger(a_db, a, 200, sim, events=later)
    _exchange(a, "a" * 64, b)
    assert b.drain_pending_signatures() == 1
    assert _rows(b_db) == [(stranger.public_hex, "early")]
    assert _quarantine(b_db) == []


def test_a_claim_and_the_row_it_authorizes_land_from_one_batch(pair):
    a_db, a, b_db, b = pair
    sim = Sim()
    _member(sim, "member")
    _write_ledger(a_db, a, 10, sim)
    _write_ledger(b_db, b, 11, sim)
    known = list(sim.ledger.events())
    newcomer = _member(sim, "member")
    later = [e for e in sim.ledger.events() if e not in known]
    row = _signed_row(sim.genesis_id, newcomer, newcomer.public_hex, {"v": "same batch"}, 1_000)
    # One transaction: the signed row is inserted BEFORE the claim events
    # that authorize it. The receiver applies ledger rows first.
    with a.transaction(100, "together"):
        a_db.conn.execute(
            "INSERT INTO settings (id,set_id,schema_revision,key,payload,"
            "publication_state,deprecated,successor_id,signed_at,signing_key,"
            "signature,witness,terminal_persona) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row,
        )
        for event_row in _event_rows(sim, later):
            a_db.conn.execute(
                "INSERT INTO settings (id,set_id,schema_revision,key,payload,publication_state)"
                " VALUES (?,?,?,?,?,?)", event_row,
            )
    _exchange(a, "a" * 64, b)
    assert _rows(b_db) == [(newcomer.public_hex, "same batch")]
    assert _quarantine(b_db) == []


def test_a_revoked_key_and_a_narrowed_role_are_refused(pair):
    a_db, a, b_db, b = pair
    sim = Sim()
    member = _member(sim, "member")
    revoked = _delegate(sim, member)
    sim.revoke_key(sim.root, revoked)
    narrowed = _member(sim, "editor", scope_set=[settings_sign_scope(OTHER_SET)])
    _write_ledger(a_db, a, 10, sim)
    _write_ledger(b_db, b, 11, sim)
    _write_signed(a_db, a, 100, _signed_row(sim.genesis_id, revoked, member.public_hex, {"v": "revoked"}, 1_000))
    _write_signed(a_db, a, 101, _signed_row(sim.genesis_id, narrowed, narrowed.public_hex, {"v": "narrowed"}, 1_000))
    _exchange(a, "a" * 64, b)
    assert _rows(b_db) == []
    parked = _quarantine(b_db)
    # A revoked delegation drops out of the fold's delegation view: the key
    # is refused as unknown or as revoked (boundary S1 accepts both).
    assert parked in (
        [("settings_signer_key_revoked", 0), ("settings_signer_lacks_settings_sign", 0)],
        [("settings_signer_lacks_settings_sign", 0), ("settings_signer_unknown", 0)],
    )


def test_a_persona_keyed_set_requires_the_row_persona(pair):
    a_db, a, b_db, b = pair
    sim = Sim()
    member = _member(sim, "member")
    other = _member(sim, "member")
    delegate = _delegate(sim, member)
    _write_ledger(a_db, a, 10, sim)
    _write_ledger(b_db, b, 11, sim)
    own = _signed_row(sim.genesis_id, delegate, member.public_hex, {"v": "mine"}, 1_000,
                      set_id=PROFILE_SET, key=member.public_hex)
    theirs = _signed_row(sim.genesis_id, delegate, member.public_hex, {"v": "theirs"}, 1_000,
                         set_id=PROFILE_SET, key=other.public_hex)
    _write_signed(a_db, a, 100, own)
    _write_signed(a_db, a, 101, theirs)
    _exchange(a, "a" * 64, b)
    assert _rows(b_db, PROFILE_SET, member.public_hex) == [(member.public_hex, "mine")]
    assert _rows(b_db, PROFILE_SET, other.public_hex) == []
    assert _quarantine(b_db) == [("settings_signer_is_not_row_persona", 0)]


def test_a_valid_key_naming_another_members_slot_is_refused(pair):
    a_db, a, b_db, b = pair
    sim = Sim()
    member = _member(sim, "member")
    victim = _member(sim, "member")
    _write_ledger(a_db, a, 10, sim)
    _write_ledger(b_db, b, 11, sim)
    _write_signed(b_db, b, 100, _signed_row(sim.genesis_id, victim, victim.public_hex, {"v": "victim"}, 1_000))
    # From A: signed by member's own valid key, but stored in the victim's slot.
    _write_signed(a_db, a, 101, _signed_row(sim.genesis_id, member, victim.public_hex, {"v": "forged slot"}, 2_000))
    _exchange(a, "a" * 64, b)
    assert _rows(b_db) == [(victim.public_hex, "victim")]
    assert _quarantine(b_db) == [("settings_signer_persona_mismatch", 0)]


def test_a_replayed_older_statement_is_stale_and_never_forwarded(pair):
    a_db, a, b_db, b = pair
    sim = Sim()
    member = _member(sim, "member")
    _write_ledger(a_db, a, 10, sim)
    _write_ledger(b_db, b, 11, sim)
    newer = _signed_row(sim.genesis_id, member, member.public_hex, {"v": "newer"}, 2_000)
    _write_signed(a_db, a, 100, newer)
    _exchange(a, "a" * 64, b)
    assert _rows(b_db) == [(member.public_hex, "newer")]
    # The same signer's OLDER statement re-sent later (a replay): a newer
    # arrival timestamp, an older signed_at.
    older = _signed_row(sim.genesis_id, member, member.public_hex, {"v": "older"}, 1_000, row_id=newer[0])
    with a.transaction(200, "replay"):
        a_db.conn.execute("DELETE FROM settings WHERE id=?", (newer[0],))
        a_db.conn.execute(
            "INSERT INTO settings (id,set_id,schema_revision,key,payload,"
            "publication_state,deprecated,successor_id,signed_at,signing_key,"
            "signature,witness,terminal_persona) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", older,
        )
    _exchange(a, "a" * 64, b)
    assert _rows(b_db) == [(member.public_hex, "newer")]
    assert _quarantine(b_db) == [("settings_signer_stale", 0)]
    assert _forwarded(b_db, b, "a" * 64, "replay") == 0
    # Final: the drain does not retry it.
    assert b.drain_pending_signatures() == 0
    assert _quarantine(b_db) == [("settings_signer_stale", 0)]


def test_unsigned_rows_are_refused_only_behind_the_flag(pair, monkeypatch):
    a_db, a, b_db, b = pair
    sim = Sim()
    _member(sim, "member")
    _write_ledger(a_db, a, 10, sim)
    _write_ledger(b_db, b, 11, sim)

    def unsigned(ts, key, value):
        with a.transaction(ts, f"unsigned-{ts}"):
            a_db.conn.execute(
                "INSERT INTO settings (id,set_id,schema_revision,key,payload,publication_state)"
                " VALUES (?,?,1,?,?,'raw')", (str(uuid.uuid4()), SET_ID, key, json.dumps({"v": value})),
            )

    monkeypatch.setattr(materialize_module, "REQUIRE_SIGNED_ORG_ROWS", False)
    unsigned(100, "k-off", "flag off")
    _exchange(a, "a" * 64, b)
    assert _rows(b_db, SET_ID, "k-off") == [(None, "flag off")]
    monkeypatch.setattr(materialize_module, "REQUIRE_SIGNED_ORG_ROWS", True)
    unsigned(200, "k-on", "flag on")
    known = list(sim.ledger.events())
    _member(sim, "member")   # ledger rows are unsigned too, and always exempt
    _write_ledger(a_db, a, 201, sim, events=[e for e in sim.ledger.events() if e not in known])
    _exchange(a, "a" * 64, b)
    assert _rows(b_db, SET_ID, "k-on") == []
    assert _quarantine(b_db) == [("settings_unsigned", 0)]
    assert b_db.conn.execute(
        "SELECT COUNT(*) FROM settings WHERE set_id=?", (LEDGER_EVENT_SET_ID,)
    ).fetchone()[0] == len(sim.ledger.events())


def test_the_fold_is_rebuilt_only_when_the_ledger_advances(tmp_path):
    db, catalog = _open(tmp_path / "a.db", "a" * 64)
    try:
        sim = Sim()
        _member(sim, "member")
        _write_ledger(db, catalog, 10, sim)
        first = authority.store_fold(db.conn)
        assert first is not None and authority.store_fold(db.conn) is first
        known = list(sim.ledger.events())
        _member(sim, "member")
        _write_ledger(db, catalog, 20, sim, events=[e for e in sim.ledger.events() if e not in known])
        second = authority.store_fold(db.conn)
        assert second is not first and len(second.members) == len(first.members) + 1
        assert authority.store_fold(db.conn) is second
    finally:
        db.close()


def test_key_strategy_resolution():
    assert authority.signing_key_strategy(PROFILE_SET, 1) == "persona"
    assert authority.signing_key_strategy(SET_ID, 1) == "delegate"
    assert authority.signing_key_strategy("autonomy.org.primer", 1) == "delegate"


def test_deleting_a_signed_row_tombstones_its_slot_only(pair):
    """Live 2026-09-29 18:37Z: the first tombstone of a signed base row
    raised ValueError (a strict zip of the five-column policy key against
    the six-part signed address) and stopped SJC-2's autonomy pull on every
    round. The tombstone deletes exactly the signer's slot."""
    a_db, a, b_db, b = pair
    sim = Sim()
    member = _member(sim, "member")
    other = _member(sim, "member")
    _write_ledger(a_db, a, 10, sim)
    _write_ledger(b_db, b, 11, sim)
    mine = _signed_row(sim.genesis_id, member, member.public_hex, {"v": "mine"}, 1_000)
    theirs = _signed_row(sim.genesis_id, other, other.public_hex, {"v": "theirs"}, 1_000)
    _write_signed(a_db, a, 100, mine)
    _write_signed(a_db, a, 101, theirs)
    _exchange(a, "a" * 64, b)
    assert _rows(b_db) == sorted([(member.public_hex, "mine"), (other.public_hex, "theirs")])
    with a.transaction(200, "remove-mine"):
        a_db.conn.execute("DELETE FROM settings WHERE id=?", (mine[0],))
    _exchange(a, "a" * 64, b)
    assert _rows(b_db) == [(other.public_hex, "theirs")]
    assert _quarantine(b_db) == []


def test_signing_an_existing_row_in_place_moves_it_to_its_signer_slot(pair, monkeypatch):
    """The one-time signing pass (S4) UPDATEs an unsigned row in place; the
    address gains the signer slot, so it replicates as a tombstone of the
    unsigned address and an insert at the signed one, under one row id.
    The pre-migration world: unsigned rows still land."""
    monkeypatch.setattr(materialize_module, "REQUIRE_SIGNED_ORG_ROWS", False)
    a_db, a, b_db, b = pair
    sim = Sim()
    member = _member(sim, "member")
    _write_ledger(a_db, a, 10, sim)
    _write_ledger(b_db, b, 11, sim)
    row_id = str(uuid.uuid4())
    with a.transaction(100, "unsigned"):
        a_db.conn.execute(
            "INSERT INTO settings (id,set_id,schema_revision,key,payload,publication_state)"
            " VALUES (?,?,1,?,?,'published')", (row_id, SET_ID, KEY, json.dumps({"v": "plain"})),
        )
    _exchange(a, "a" * 64, b)
    assert _rows(b_db) == [(None, "plain")]
    signed = _signed_row(sim.genesis_id, member, member.public_hex, {"v": "plain"}, 1_000, row_id=row_id)
    with a.transaction(200, "sign-in-place"):
        a_db.conn.execute(
            "UPDATE settings SET signed_at=?, signing_key=?, signature=?, witness=?, terminal_persona=? WHERE id=?",
            (signed[8], signed[9], signed[10], signed[11], signed[12], row_id),
        )
    _exchange(a, "a" * 64, b)
    assert _rows(b_db) == [(member.public_hex, "plain")]
    assert b_db.conn.execute("SELECT COUNT(*) FROM settings WHERE id=?", (row_id,)).fetchone()[0] == 1
    assert _quarantine(b_db) == []


def test_an_envelope_this_code_cannot_read_is_parked_forwarded_and_lands_after_the_update(pair, monkeypatch):
    """Live 2026-09-29 18:59-19:06Z: SJC-2, on the pre-float encoder, could
    not rebuild envelopes Home had signed with float payloads and filed
    14,362 valid rows under the final reason. An envelope THIS code cannot
    read is its own, drainable reason."""
    from tools.network.settingskit import envelope as envelope_module
    from tools.network.settingskit.envelope import EnvelopeFormatError

    a_db, a, b_db, b = pair
    sim = Sim()
    member = _member(sim, "member")
    _write_ledger(a_db, a, 10, sim)
    _write_ledger(b_db, b, 11, sim)
    _write_signed(a_db, a, 100, _signed_row(sim.genesis_id, member, member.public_hex, {"v": "float-ish"}, 1_000))
    real = envelope_module.record_from_row

    def behind_encoder(row, org):
        raise EnvelopeFormatError("floats are not allowed in canonical idkit JSON")

    monkeypatch.setattr(materialize_module, "_verify_settings_row", materialize_module._verify_settings_row)
    monkeypatch.setattr(envelope_module, "record_from_row", behind_encoder)
    _exchange(a, "a" * 64, b)
    assert _rows(b_db) == []
    assert _quarantine(b_db) == [("settings_envelope_unreadable", 0)]
    assert _forwarded(b_db, b, "a" * 64, "signed-100") == 1   # travels on: the receiver is behind, not the row
    assert b.drain_pending_signatures() == 0
    assert _quarantine(b_db) == [("settings_envelope_unreadable", 1)]
    # The code update: the envelope reads again, the next drain lands it.
    monkeypatch.setattr(envelope_module, "record_from_row", real)
    assert b.drain_pending_signatures() == 1
    assert _rows(b_db) == [(member.public_hex, "float-ish")]
    assert _quarantine(b_db) == []


def test_an_already_parked_invalid_row_is_re_judged_exactly_once(pair, monkeypatch):
    """Rows parked as settings_signature_invalid before the unreadable
    reason existed: a valid one lands on the first drain of the new code;
    a tampered one stays invalid, counts its one retry, and is never
    re-judged again."""
    from tools.network.settingskit import envelope as envelope_module
    from tools.network.settingskit.envelope import EnvelopeFormatError

    a_db, a, b_db, b = pair
    sim = Sim()
    member = _member(sim, "member")
    _write_ledger(a_db, a, 10, sim)
    _write_ledger(b_db, b, 11, sim)
    honest = _signed_row(sim.genesis_id, member, member.public_hex, {"v": "honest"}, 1_000)
    forged = _signed_row(sim.genesis_id, member, member.public_hex, {"v": "forged"}, 1_000, key="other-key")
    _write_signed(a_db, a, 100, honest)
    _write_signed(a_db, a, 101, forged)
    with a.transaction(150, "tamper"):
        a_db.conn.execute("UPDATE settings SET payload=? WHERE id=?", (json.dumps({"v": "tampered"}), forged[0]))
    # The receiver is behind the writer's encoder when both arrive ...
    real = envelope_module.record_from_row
    monkeypatch.setattr(envelope_module, "record_from_row",
                        lambda row, org: (_ for _ in ()).throw(EnvelopeFormatError("floats are not allowed")))
    _exchange(a, "a" * 64, b)
    assert _rows(b_db) == []
    assert _quarantine(b_db) == [("settings_envelope_unreadable", 0), ("settings_envelope_unreadable", 0)]
    # ... and the code of the day filed both under the FINAL reason.
    with b_db.conn:
        b_db.conn.execute("UPDATE fleet_sync_quarantine SET reason='settings_signature_invalid'")
    monkeypatch.setattr(envelope_module, "record_from_row", real)
    assert b.drain_pending_signatures() == 1          # the honest one lands ...
    assert _rows(b_db) == [(member.public_hex, "honest")]
    assert _quarantine(b_db) == [("settings_signature_invalid", 1)]   # ... the forgery counted its one retry
    assert b.drain_pending_signatures() == 0          # and is never re-judged again
    assert _quarantine(b_db) == [("settings_signature_invalid", 1)]
