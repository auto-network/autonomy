"""Attributed write paths for the ``mission`` plugin.

Every mutation is a settings read-modify-write against the plugin's own
sets, validated by the registered schemas at the write boundary. The
verbs encode the doctrine directly:

* a chat message joins the pillar's untracked log — the human's only
  input surface;
* a question REPLY is not an answer — it appends to the discussion;
  ``progress`` entries are the transient status the working agent posts;
  the ANSWER is the one cohesive resolution that closes the question;
* a checkpoint state transition appends to ``history`` (the trail the
  upserting store cannot keep itself) and confirmation stamps
  provenance; ``work`` entries are the attributed progress stream.

Attribution comes from the API principal established at the boundary —
never from the request body.
"""
from __future__ import annotations

from datetime import datetime, timezone

from tools.dashboard.plugins.mission.entrypoints.schemas import (
    CHAT_SET_ID,
    ITEM_SET_ID,
    SCHEMA_REVISION,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ops():
    from tools.graph import ops as graph_ops
    return graph_ops


class WriteRefused(Exception):
    """A domain rule refused the write; message is operator-readable."""


def _item_key(mission_id: str, pillar_id: str, item_id: str) -> str:
    return f"{mission_id}:{pillar_id}:{item_id}"


def _load_item(org: str, key: str) -> dict:
    row = _ops().read_set_key(ITEM_SET_ID, key, org=org or None, peers=[])
    if row is None:
        raise WriteRefused(f"no item {key.split(':', 1)[1]!r} on that pillar")
    return dict(row)


def _store_item(org: str, key: str, payload: dict) -> None:
    _ops().add_setting(ITEM_SET_ID, SCHEMA_REVISION, key, payload,
                       org=org or None)


def upsert_item(org: str, mission_id: str, pillar_id: str, item_id: str,
                payload: dict) -> dict:
    """Full-payload create-or-rewrite; schema validation is the gate."""
    key = _item_key(mission_id, pillar_id, item_id)
    _store_item(org, key, payload)
    return payload


def transition(org: str, mission_id: str, pillar_id: str, item_id: str,
               *, to_state: str, by: str, at: str | None = None,
               turn: int | None = None) -> dict:
    """Checkpoint state change: history appended, confirmation stamped."""
    key = _item_key(mission_id, pillar_id, item_id)
    item = _load_item(org, key)
    if item.get("kind") != "checkpoint":
        raise WriteRefused("state transitions apply to checkpoints; "
                           "questions resolve through their answer")
    prev = item.get("state", "")
    if to_state == prev:
        raise WriteRefused(f"already {to_state}")
    moment = at or now_iso()
    item.setdefault("history", []).append(
        {"from": prev, "to": to_state, "at": moment, "by": by})
    item["state"] = to_state
    if to_state == "confirmed":
        item["confirmed_by"] = by
        item["confirmed_at"] = moment
        if turn:
            item["confirmed_turn"] = int(turn)
    _store_item(org, key, item)
    return item


def add_work(org: str, mission_id: str, pillar_id: str, item_id: str,
             *, text: str, by: str) -> dict:
    """Append one attributed work entry to a checkpoint."""
    key = _item_key(mission_id, pillar_id, item_id)
    item = _load_item(org, key)
    if item.get("kind") != "checkpoint":
        raise WriteRefused("work entries belong to checkpoints")
    item.setdefault("work", []).append(
        {"by": by, "at": now_iso(), "text": text})
    _store_item(org, key, item)
    return item


def add_discussion(org: str, mission_id: str, pillar_id: str, item_id: str,
                   *, text: str, by: str, progress: bool = False) -> dict:
    """A reply (or transient progress entry) on an open question."""
    key = _item_key(mission_id, pillar_id, item_id)
    item = _load_item(org, key)
    if item.get("kind") != "question":
        raise WriteRefused("discussion belongs to questions")
    if item.get("state") != "open":
        raise WriteRefused("this question is answered; reopen it by "
                           "asking a new question")
    entry = {"by": by, "at": now_iso(), "text": text}
    if progress:
        entry["type"] = "progress"
    item.setdefault("discussion", []).append(entry)
    _store_item(org, key, item)
    return item


def answer_question(org: str, mission_id: str, pillar_id: str, item_id: str,
                    *, text: str, by: str) -> dict:
    """The one cohesive resolution: closes the question, clears blockage."""
    key = _item_key(mission_id, pillar_id, item_id)
    item = _load_item(org, key)
    if item.get("kind") != "question":
        raise WriteRefused("only questions take answers")
    if item.get("state") != "open":
        raise WriteRefused("already answered")
    item["answer"] = {"text": text, "by": by, "at": now_iso()}
    item["state"] = "answered"
    _store_item(org, key, item)
    return item


def add_chat(org: str, mission_id: str, pillar_id: str,
             *, text: str, by: str) -> list[dict]:
    """Append one message to the pillar's chat log; returns the log."""
    key = f"{mission_id}:{pillar_id}"
    row = _ops().read_set_key(CHAT_SET_ID, key, org=org or None, peers=[])
    entries = list((row or {}).get("entries") or [])
    entries.append({"by": by, "at": now_iso(), "text": text})
    _ops().add_setting(CHAT_SET_ID, SCHEMA_REVISION, key,
                       {"entries": entries}, org=org or None)
    return entries
