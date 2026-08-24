"""Attributed write verbs (writes.py) against an in-memory settings
store that enforces the real registered schemas on every write — so a
verb that would store an invalid item fails here exactly as it would at
the settings boundary.
"""
from __future__ import annotations

import pytest

from tools.dashboard.plugins.mission import writes
from tools.dashboard.plugins.mission.entrypoints import schemas as S
from tools.graph.schemas.registry import validate_payload

MID = "m-uuid"


class _Member:
    def __init__(self, key, payload, updated_at="2026-08-24T00:00:00Z"):
        self.key = key
        self.payload = payload
        self.updated_at = updated_at


class FakeOps:
    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.stale_twins: list[_Member] = []

    def read_set(self, set_id, *, org, peers=None):
        out = [_Member(k, p) for (sid, k), p in self.rows.items()
               if sid == set_id]
        return out + [m for m in self.stale_twins]

    def upsert_by_key(self, set_id, revision, key, payload, *, org,
                      state="raw"):
        validate_payload(set_id, revision, payload)   # the real gate
        self.rows[(set_id, key)] = dict(payload)
        return key

    def add_setting(self, set_id, revision, key, payload, *, org,
                    state="raw"):
        """Append-only semantics, as the real one: a second write at
        the same key is a UNIQUE violation, never an update."""
        validate_payload(set_id, revision, payload)
        if (set_id, key) in self.rows:
            raise ValueError("UNIQUE constraint failed (fake)")
        self.rows[(set_id, key)] = dict(payload)
        return key


@pytest.fixture()
def store(monkeypatch):
    fake = FakeOps()
    monkeypatch.setattr(writes, "_ops", lambda: fake)
    return fake


def _seed(store, item_id, payload):
    store.rows[(S.ITEM_SET_ID, f"{MID}:relay:{item_id}")] = payload


def test_verbs_resolve_newest_over_stale_duplicates(store):
    """The live bug: a stale twin row (pre-upsert write path) must never
    outvote the fresh row in a verb's kind check."""
    _seed(store, "q", {"kind": "question", "state": "open", "title": "t"})
    store.stale_twins.append(_Member(
        f"{MID}:relay:q",
        {"kind": "decision", "title": "stale twin", "chosen": "x"},
        updated_at="2026-08-20T00:00:00Z"))
    item = writes.answer_question("o", MID, "relay", "q",
                                  text="resolved", by="auto-x")
    assert item["state"] == "answered"


class TestCheckpoints:
    def test_transition_appends_history_and_stamps_confirmation(self, store):
        _seed(store, "crit", {"kind": "checkpoint", "state": "in_progress",
                              "title": "t"})
        item = writes.transition(
            "o", MID, "relay", "crit",
            to_state="confirmed", by="auto-1", turn=42)
        assert item["state"] == "confirmed"
        h = item["history"][-1]
        assert (h["from"], h["to"], h["by"]) == ("in_progress", "confirmed",
                                                 "auto-1")
        assert item["confirmed_by"] == "auto-1"
        assert item["confirmed_turn"] == 42
        # persisted, not just returned
        stored = store.rows[(S.ITEM_SET_ID, f"{MID}:relay:crit")]
        assert stored["state"] == "confirmed"

    def test_same_state_and_non_checkpoint_refused(self, store):
        _seed(store, "crit", {"kind": "checkpoint", "state": "pending",
                              "title": "t"})
        with pytest.raises(writes.WriteRefused):
            writes.transition("o", MID, "relay", "crit",
                              to_state="pending", by="s")
        _seed(store, "q", {"kind": "question", "state": "open", "title": "t"})
        with pytest.raises(writes.WriteRefused):
            writes.transition("o", MID, "relay", "q",
                              to_state="confirmed", by="s")

    def test_work_appends_only_on_checkpoints(self, store):
        _seed(store, "crit", {"kind": "checkpoint", "state": "in_progress",
                              "title": "t"})
        item = writes.add_work("o", MID, "relay", "crit",
                               text="fixed the retry window", by="auto-1")
        assert item["work"][-1]["by"] == "auto-1"
        _seed(store, "d", {"kind": "decision", "title": "t", "chosen": "x"})
        with pytest.raises(writes.WriteRefused):
            writes.add_work("o", MID, "relay", "d", text="w", by="s")


class TestQuestions:
    def test_reply_is_not_an_answer(self, store):
        _seed(store, "q", {"kind": "question", "state": "open", "title": "t"})
        item = writes.add_discussion("o", MID, "relay", "q",
                                     text="what about TTL?", by="Jeremy")
        assert item["state"] == "open"
        assert item["discussion"][-1] == {
            "by": "Jeremy", "at": item["discussion"][-1]["at"],
            "text": "what about TTL?"}

    def test_progress_entries_carry_type(self, store):
        _seed(store, "q", {"kind": "question", "state": "open", "title": "t"})
        item = writes.add_discussion("o", MID, "relay", "q",
                                     text="checking the cache path",
                                     by="auto-1", progress=True)
        assert item["discussion"][-1]["type"] == "progress"

    def test_answer_closes_and_further_replies_refused(self, store):
        _seed(store, "q", {"kind": "question", "state": "open", "title": "t",
                           "blocking": True})
        item = writes.answer_question("o", MID, "relay", "q",
                                      text="Epoch bump refuses replays.",
                                      by="auto-1")
        assert item["state"] == "answered"
        assert item["answer"]["by"] == "auto-1"
        with pytest.raises(writes.WriteRefused):
            writes.add_discussion("o", MID, "relay", "q", text="more", by="x")
        with pytest.raises(writes.WriteRefused):
            writes.answer_question("o", MID, "relay", "q", text="again",
                                   by="x")

    def test_missing_item_named_in_refusal(self, store):
        with pytest.raises(writes.WriteRefused) as exc:
            writes.add_discussion("o", MID, "relay", "ghost", text="x", by="y")
        assert "ghost" in str(exc.value)


class TestChatAndUpsert:
    def test_chat_is_one_signable_row_per_message(self, store):
        persona = "5ff2d4e2" * 8               # a member persona pub key
        entries = writes.add_chat("o", MID, "relay",
                                  text="What's going on?", by=persona)
        assert entries[-1]["by"] == persona
        entries = writes.add_chat("o", MID, "relay", text="Update?",
                                  by="auto-relay")
        assert len(entries) == 2
        msg_rows = [k for (sid, k) in store.rows
                    if sid == S.CHAT_SET_ID
                    and k.startswith(f"{MID}:relay:")]
        assert len(msg_rows) == 2              # one row per message
        assert entries[0]["text"] == "What's going on?"   # oldest first

    def test_upsert_is_schema_gated(self, store):
        writes.upsert_item("o", MID, "relay", "sc",
                           {"kind": "scope", "title": "Charter"})
        # re-PUT of the same key is an update, never a collision
        writes.upsert_item("o", MID, "relay", "sc",
                           {"kind": "scope", "title": "Charter v2"})
        assert store.rows[(S.ITEM_SET_ID, f"{MID}:relay:sc")]["title"] \
            == "Charter v2"
        with pytest.raises(Exception):
            writes.upsert_item("o", MID, "relay", "bad",
                               {"kind": "status", "state": "confirmed",
                                "title": "t"})
        assert (S.ITEM_SET_ID, f"{MID}:relay:bad") not in store.rows
