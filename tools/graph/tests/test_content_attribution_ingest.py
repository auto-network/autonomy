"""The ingest write-path stamps persona_id + session_id.

cb06ec51 added the columns and wired them into the API middleware only; the
ingest path (which produces the bulk of session content) constructed every
Thought/Derivation with the fields defaulting to None, so a re-ingest would
recreate the NULL-stamp gap at scale. `_write_new_turns` now threads
persona_id + the session UUID through to every row.
"""
from __future__ import annotations

from tools.graph.db import GraphDB
from tools.graph.ingest import _write_new_turns
from tools.graph.models import Source


def _db_with_source(tmp_path):
    db = GraphDB(tmp_path / "org.db")
    source = Source(type="session", platform="codex-cli", title="t",
                    file_path="/tmp/t.jsonl")
    db.insert_source(source)
    return db, source.id


def test_write_new_turns_stamps_persona_and_session(tmp_path):
    db, source_id = _db_with_source(tmp_path)
    persona = "b97814be579cfd035ad7b7b48184f1e89fb176c2135af5eaf434d34facc7acb3"
    session_uuid = "4089b19b-3052-4f40-b98d-a1470c9c9843"  # the JSONL stem
    turns = [
        {"turn_number": 1, "message_id": "m1", "content": "a directive",
         "role": "user"},
        {"turn_number": 2, "message_id": "m2", "content": "an AI reply",
         "role": "assistant"},
    ]
    thoughts, derivations, _ = _write_new_turns(
        db, source_id, turns, model="claude",
        persona_id=persona, session_id=session_uuid,
    )
    assert len(thoughts) == 1 and len(derivations) == 1
    # Thoughts (operator turns) carry persona_id + session_id in-memory...
    assert thoughts[0].persona_id == persona
    assert thoughts[0].session_id == session_uuid
    # ...derivations (AI replies) deliberately do NOT — there are no
    # persona_id/session_id columns on the derivations table; a derivation's
    # session is inherited via its source_id/thought_id.
    assert not hasattr(derivations[0], "persona_id")
    assert not hasattr(derivations[0], "session_id")
    # ...and the thought values persisted to the DB (NOT NULL — the point of the fix).
    trow = db.conn.execute(
        "SELECT persona_id, session_id FROM thoughts WHERE source_id=?",
        (source_id,),
    ).fetchone()
    assert trow[0] == persona
    assert trow[1] == session_uuid
    db.close()


def test_write_new_turns_defaults_none_when_unstamped(tmp_path):
    """Callers that don't pass persona/session still work (fields default None,
    no crash) — backward compatibility."""
    db, source_id = _db_with_source(tmp_path)
    turns = [{"turn_number": 1, "message_id": "m1", "content": "x",
              "role": "user"}]
    thoughts, _, _ = _write_new_turns(db, source_id, turns, model=None)
    assert thoughts[0].persona_id is None
    assert thoughts[0].session_id is None
    db.close()
