"""Direct unit tests for _dedup_new_turns (auto-4y579 within-batch stage).

The existing-DB stage (auto-cpg1x) is exercised at a higher level in
test_session_ingest_codex.py's renumbering regression tests. This file
targets the within-batch stage directly: a codex rollout can double-emit
the same event (~5ms apart), so two copies of the same message_id can
arrive in a SINGLE batch, before either has been written — the
existing-DB check alone can't catch that, since neither copy is in the
DB yet when either is checked.
"""

from __future__ import annotations

from tools.graph.db import GraphDB
from tools.graph.ingest import _dedup_new_turns
from tools.graph.models import Source


def _turn(turn_number: int, message_id: str | None, content: str = "x") -> dict:
    return {"turn_number": turn_number, "message_id": message_id, "content": content, "role": "user"}


def _db_with_source(tmp_path) -> tuple[GraphDB, str]:
    db = GraphDB(tmp_path / "org.db")
    source = Source(type="session", platform="codex-cli", title="t", file_path="/tmp/t.jsonl")
    db.insert_source(source)
    return db, source.id


class TestWithinBatchDedup:
    def test_duplicate_id_within_one_batch_keeps_first_only(self, tmp_path):
        db, source_id = _db_with_source(tmp_path)
        turns = [
            _turn(1, "dup", content="first copy"),
            _turn(2, "dup", content="double-emitted copy, 5ms later"),
        ]
        result = _dedup_new_turns(db, source_id, turns, max_turn=0)
        assert len(result) == 1
        assert result[0]["content"] == "first copy"
        db.close()

    def test_keeps_file_order_not_lowest_turn_number(self, tmp_path):
        """Codex double-emission duplicates are adjacent in file order —
        the fix keeps whichever copy appears FIRST while scanning, not
        whichever has the lowest turn_number (they're consecutive, so in
        practice this is the same thing, but the contract is order-of-
        appearance, matching how _write_new_turns consumes the list)."""
        db, source_id = _db_with_source(tmp_path)
        turns = [
            _turn(5, "dup", content="appears first in the batch"),
            _turn(6, "dup", content="appears second in the batch"),
        ]
        result = _dedup_new_turns(db, source_id, turns, max_turn=0)
        assert len(result) == 1
        assert result[0]["content"] == "appears first in the batch"
        db.close()

    def test_triple_emission_keeps_only_one(self, tmp_path):
        db, source_id = _db_with_source(tmp_path)
        turns = [_turn(i, "dup") for i in range(1, 4)]
        result = _dedup_new_turns(db, source_id, turns, max_turn=0)
        assert len(result) == 1
        db.close()

    def test_null_message_id_turns_never_dedup_against_each_other(self, tmp_path):
        db, source_id = _db_with_source(tmp_path)
        turns = [_turn(1, None), _turn(2, None), _turn(3, None)]
        result = _dedup_new_turns(db, source_id, turns, max_turn=0)
        assert len(result) == 3
        db.close()

    def test_distinct_ids_in_same_batch_all_kept(self, tmp_path):
        db, source_id = _db_with_source(tmp_path)
        turns = [_turn(1, "a"), _turn(2, "b"), _turn(3, "c")]
        result = _dedup_new_turns(db, source_id, turns, max_turn=0)
        assert len(result) == 3
        db.close()

    def test_within_batch_dedup_combines_with_existing_db_check(self, tmp_path):
        """A batch can have BOTH kinds of duplicate at once: one id
        already committed from an earlier pass, one id double-emitted
        within this batch."""
        db, source_id = _db_with_source(tmp_path)
        from tools.graph.models import Thought
        db.insert_thought(Thought(source_id=source_id, content="already there", turn_number=1, message_id="old"))
        db.commit()

        turns = [
            _turn(2, "old", content="renumbered duplicate of the old one"),
            _turn(3, "new", content="genuinely new, first copy"),
            _turn(4, "new", content="genuinely new, double-emitted copy"),
        ]
        result = _dedup_new_turns(db, source_id, turns, max_turn=1)
        assert len(result) == 1
        assert result[0]["content"] == "genuinely new, first copy"
        db.close()

    def test_no_candidates_above_max_turn_short_circuits_without_db_query(self, tmp_path):
        db, source_id = _db_with_source(tmp_path)
        turns = [_turn(1, "a"), _turn(2, "b")]
        result = _dedup_new_turns(db, source_id, turns, max_turn=10)
        assert result == []
        db.close()
