"""The settings table carries the signed envelope, and an edit breaks it.

Storage shape for auto-4oxee (design of record graph://21a0da9e-1c2): the
envelope is COLUMNS beside the existing settings columns, not a wrapper around
``payload`` — so schema validation reads the payload exactly as it does on an
unsigned row, and a stored column edited after signing no longer matches the
signed bytes.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from tools.graph.db import GraphDB
from tools.network.idkit import derive_persona
from tools.network.idkit.errors import SignatureError
from tools.network.settingskit import (
    build_record,
    record_from_row,
    sign_record,
    verify_record,
)

ENVELOPE_COLUMNS = (
    "signed_at", "signing_key", "signature", "witness", "terminal_persona",
)
GENESIS = "1f" * 32
PERSONA = derive_persona(bytes(range(32)), GENESIS)


def settings_columns(conn) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(settings)").fetchall()}


def test_a_fresh_org_db_has_the_envelope_columns(tmp_path):
    db = GraphDB.create_org_db("enveloped", path=tmp_path / "enveloped.db")
    try:
        assert set(ENVELOPE_COLUMNS) <= settings_columns(db.conn)
    finally:
        db.close()


def test_a_legacy_settings_table_gains_the_columns_on_open(tmp_path):
    """A pre-envelope database migrates in place, losing nothing."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE settings ("
        " id TEXT PRIMARY KEY, set_id TEXT NOT NULL,"
        " schema_revision INTEGER NOT NULL, key TEXT NOT NULL,"
        " payload TEXT NOT NULL,"
        " publication_state TEXT NOT NULL DEFAULT 'raw',"
        " supersedes TEXT, excludes TEXT,"
        " deprecated INTEGER NOT NULL DEFAULT 0, successor_id TEXT,"
        " created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),"
        " updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        ")"
    )
    conn.execute(
        "INSERT INTO settings (id, set_id, schema_revision, key, payload)"
        " VALUES ('pre', 'a.set', 1, 'k', '{}')"
    )
    conn.commit()
    conn.close()

    db = GraphDB(path)
    try:
        assert set(ENVELOPE_COLUMNS) <= settings_columns(db.conn)
        survivor = db.conn.execute(
            "SELECT payload, signature FROM settings WHERE id = 'pre'"
        ).fetchone()
        assert survivor["payload"] == "{}"
        assert survivor["signature"] is None
    finally:
        db.close()


@pytest.fixture
def signed_row_db(tmp_path):
    """An org DB holding one signed row, stored the way the boundary will."""
    db = GraphDB.create_org_db("signedorg", path=tmp_path / "signedorg.db")
    record = build_record(
        org=GENESIS,
        set_id="autonomy.org.member-directory",
        key=PERSONA.public_hex,
        schema_revision=1,
        publication_state="published",
        deprecated=False,
        successor_id=None,
        payload={"display_name": "Ada ☃"},
        signed_at=1_755_500_000_123,
        signing_key=PERSONA.public_hex,
        witness=None,
    )
    sig = sign_record(PERSONA, record)
    db.conn.execute(
        "INSERT INTO settings (id, set_id, schema_revision, key, payload,"
        " publication_state, deprecated, successor_id, signed_at, signing_key,"
        " signature, witness, terminal_persona)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()), record["set_id"], record["schema_revision"],
            record["key"], json.dumps(record["payload"], ensure_ascii=False),
            record["publication_state"], int(record["deprecated"]),
            record["successor_id"], record["signed_at"], record["signing_key"],
            sig, None, PERSONA.public_hex,
        ),
    )
    db.conn.commit()
    try:
        yield db, record
    finally:
        db.close()


def fetch_row(db):
    return db.conn.execute("SELECT * FROM settings").fetchone()


def test_a_stored_signed_row_re_verifies_from_its_columns(signed_row_db):
    db, record = signed_row_db
    row = fetch_row(db)
    rebuilt = record_from_row(row, GENESIS)
    verify_record(rebuilt, row["signature"])
    # Columns, not a wrapper: the payload column IS the schema-validated
    # object, byte-comparable to what an unsigned row would hold.
    assert json.loads(row["payload"]) == record["payload"]


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE settings SET publication_state = 'canonical'",
        "UPDATE settings SET deprecated = 1",
        "UPDATE settings SET payload = '{\"display_name\": \"Eve\"}'",
        "UPDATE settings SET signed_at = signed_at + 1",
    ],
)
def test_an_edited_signed_column_fails_verification(signed_row_db, tamper):
    db, _ = signed_row_db
    db.conn.execute(tamper)
    db.conn.commit()
    row = fetch_row(db)
    with pytest.raises(SignatureError):
        verify_record(record_from_row(row, GENESIS), row["signature"])


# --- a row is unsigned or signed; the 29 states in between do not store -----

ENVELOPE_VALUES = {
    "signed_at": 1_755_500_000_123,
    "signing_key": PERSONA.public_hex,
    "signature": "ab" * 64,
    "witness": '{"entry": {}}',
    "terminal_persona": PERSONA.public_hex,
}


def _valid_pattern(present: set[str]) -> bool:
    if not present:
        return True  # unsigned
    return {"signed_at", "signing_key", "signature", "terminal_persona"} <= present


def _all_patterns():
    columns = list(ENVELOPE_VALUES)
    for mask in range(2 ** len(columns)):
        yield {columns[i] for i in range(len(columns)) if mask >> i & 1}


def _assert_envelope_state_invariant(db):
    accepted, refused = [], []
    for present in _all_patterns():
        row_id = "pattern-" + "-".join(sorted(present)) if present else "pattern-none"
        values = {c: (ENVELOPE_VALUES[c] if c in present else None)
                  for c in ENVELOPE_VALUES}
        try:
            with db.conn:
                db.conn.execute(
                    "INSERT INTO settings (id, set_id, schema_revision, key,"
                    " payload, signed_at, signing_key, signature, witness,"
                    " terminal_persona) VALUES (?, 'a.set', 1, ?, '{}',"
                    " ?, ?, ?, ?, ?)",
                    (row_id, row_id, values["signed_at"], values["signing_key"],
                     values["signature"], values["witness"],
                     values["terminal_persona"]),
                )
            accepted.append(present)
        except sqlite3.IntegrityError:
            refused.append(present)
    assert [p for p in accepted if not _valid_pattern(p)] == [], (
        "an invalid partial signing state was stored"
    )
    assert [p for p in refused if _valid_pattern(p)] == [], (
        "a valid signing state was refused"
    )
    assert len(accepted) == 3  # unsigned, signed+witness, signed sans witness

    # The same invariant on the UPDATE path: partially signing a stored
    # unsigned row is refused too.
    with pytest.raises(sqlite3.IntegrityError):
        with db.conn:
            db.conn.execute(
                "UPDATE settings SET signed_at = 1 WHERE id = 'pattern-none'"
            )


def test_a_fresh_table_refuses_every_partial_signing_state(tmp_path):
    db = GraphDB.create_org_db("fresh-inv", path=tmp_path / "fresh-inv.db")
    try:
        _assert_envelope_state_invariant(db)
    finally:
        db.close()


def test_a_migrated_legacy_table_refuses_them_identically(tmp_path):
    """SQLite cannot ADD CHECK, so pre-envelope tables enforce the invariant
    through the migration's triggers — same predicate, same refusals."""
    path = tmp_path / "legacy-inv.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE settings ("
        " id TEXT PRIMARY KEY, set_id TEXT NOT NULL,"
        " schema_revision INTEGER NOT NULL, key TEXT NOT NULL,"
        " payload TEXT NOT NULL,"
        " publication_state TEXT NOT NULL DEFAULT 'raw',"
        " supersedes TEXT, excludes TEXT,"
        " deprecated INTEGER NOT NULL DEFAULT 0, successor_id TEXT,"
        " created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),"
        " updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        ")"
    )
    conn.commit()
    conn.close()
    db = GraphDB(path)
    try:
        _assert_envelope_state_invariant(db)
    finally:
        db.close()
