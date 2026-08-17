"""Graph-side persistence tests for accepted turn corrections.

Covers bead ``auto-edec1.6``: when a dashboard accept transition fires for
a session whose workspace has ``persist_accepts_to_graph=true``, the graph
side gains one new ``user`` thought in the same ingested session source as
the original turn, linked back to the original via a ``supersedes`` edge.

The tests pin the contract from every angle the bead spec calls out:

* the corrected thought body is ``corrected_text`` *verbatim*;
* the corrected thought lives in the *same* session source, never as a
  separate note or comment source;
* provenance metadata (``session_uuid``, ``target_message_id``,
  ``original_sha256``) is present on both the thought row and the
  ``supersedes`` edge so future tooling can trace the accepted correction
  back to the originating turn;
* the original thought row stays untouched;
* repeated accept handling is idempotent — the deterministic derived
  ``message_id`` plus the ``edges.UNIQUE(source_id, target_id, relation)``
  constraint guarantee no duplicate thought or duplicate edge ever lands;
* missing source / missing original thought fail closed (``None``,
  no inserts) so the dashboard never silently writes an orphaned overlay;
* Codex live sessions whose ``event_msg`` rows had no payload UUID still
  resolve, because both ingest and the live overlay derive the same
  ``codex-<role>:<sha1[:16]>`` deterministic id (the bead's identity-rule
  alignment requirement).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tools.graph import ops
from tools.graph.db import GraphDB
from tools.graph.ingest import ingest_session_file
from tools.graph.models import Source, Thought


SESSION_UUID = "uuid-supersedes-test"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_session_with_user_turn(
    db: GraphDB,
    *,
    session_uuid: str = SESSION_UUID,
    target_message_id: str = "msg-1",
    raw_text: str = "Jason encoded",
) -> tuple[str, str]:
    """Create a session source + one user thought with ``message_id``.

    Returns ``(source_id, thought_id)``.
    """
    src = Source(
        type="session",
        platform="codex-cli",
        title="seed session",
        file_path=f"/tmp/seed-{session_uuid}.jsonl",
        metadata={"session_uuid": session_uuid, "session_id": session_uuid},
    )
    db.insert_source(src)
    thought = Thought(
        source_id=src.id,
        content=raw_text,
        role="user",
        turn_number=1,
        message_id=target_message_id,
    )
    db.insert_thought(thought)
    db.commit()
    return src.id, thought.id


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin ops to a tmp ``GRAPH_DB`` so ``_open(None)`` lands in our scratch DB.

    Tests that pass ``org=None`` rely on ``GRAPH_DB`` taking precedence over
    the per-org cascade — see the module docstring of ``tools.graph.ops``.
    """
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    db = GraphDB(db_path)
    yield db
    db.close()


# ── persist_corrected_thought: happy path ─────────────────────


def test_persist_creates_superseding_thought_in_same_source(graph_db_env):
    """Accepted correction lands in the *same* session source as the original."""
    src_id, original_thought_id = _seed_session_with_user_turn(
        graph_db_env, raw_text="Jason encoded",
    )
    sha = _sha("Jason encoded")

    result = ops.persist_corrected_thought(
        org=None,
        session_uuid=SESSION_UUID,
        target_message_id="msg-1",
        original_sha256=sha,
        corrected_text="JSON encoded",
    )
    assert result is not None
    assert result["created"] is True
    assert result["source_id"] == src_id
    assert result["message_id"] == "supersedes:msg-1"

    rows = graph_db_env.conn.execute(
        "SELECT id, content, role, message_id, metadata FROM thoughts"
        " WHERE source_id = ? ORDER BY turn_number",
        (src_id,),
    ).fetchall()
    assert [r["content"] for r in rows] == ["Jason encoded", "JSON encoded"]
    assert rows[1]["role"] == "user"
    assert rows[1]["message_id"] == "supersedes:msg-1"
    meta = json.loads(rows[1]["metadata"])
    assert meta["kind"] == "turn_correction_supersedes"
    assert meta["session_uuid"] == SESSION_UUID
    assert meta["target_message_id"] == "msg-1"
    assert meta["original_thought_id"] == original_thought_id
    assert meta["original_sha256"] == sha


def test_persist_creates_supersedes_edge(graph_db_env):
    src_id, original_thought_id = _seed_session_with_user_turn(graph_db_env)
    sha = _sha("Jason encoded")

    result = ops.persist_corrected_thought(
        org=None,
        session_uuid=SESSION_UUID,
        target_message_id="msg-1",
        original_sha256=sha,
        corrected_text="JSON encoded",
    )
    assert result is not None

    edges = graph_db_env.conn.execute(
        "SELECT source_id, source_type, target_id, target_type, relation,"
        " metadata FROM edges WHERE relation = 'supersedes'",
    ).fetchall()
    assert len(edges) == 1
    edge = edges[0]
    assert edge["source_type"] == "thought"
    assert edge["target_type"] == "thought"
    assert edge["source_id"] == result["thought_id"]
    assert edge["target_id"] == original_thought_id
    edge_meta = json.loads(edge["metadata"])
    assert edge_meta["session_uuid"] == SESSION_UUID
    assert edge_meta["target_message_id"] == "msg-1"
    assert edge_meta["original_sha256"] == sha


def test_persist_does_not_mutate_original_thought(graph_db_env):
    """Bead invariant: the original graph turn remains unchanged."""
    src_id, original_thought_id = _seed_session_with_user_turn(
        graph_db_env, raw_text="Jason encoded",
    )
    sha = _sha("Jason encoded")
    before = graph_db_env.conn.execute(
        "SELECT content, role, message_id FROM thoughts WHERE id = ?",
        (original_thought_id,),
    ).fetchone()

    ops.persist_corrected_thought(
        org=None,
        session_uuid=SESSION_UUID,
        target_message_id="msg-1",
        original_sha256=sha,
        corrected_text="JSON encoded",
    )

    after = graph_db_env.conn.execute(
        "SELECT content, role, message_id FROM thoughts WHERE id = ?",
        (original_thought_id,),
    ).fetchone()
    assert dict(after) == dict(before)


def test_persist_uses_corrected_text_verbatim(graph_db_env):
    """``corrected_text`` is the full replacement message, not a diff or patch."""
    src_id, _ = _seed_session_with_user_turn(graph_db_env)
    sha = _sha("Jason encoded")
    corrected = (
        "JSON encoded payload with **markdown** and a long explanation\n"
        "spanning multiple lines so we know body is preserved verbatim."
    )

    result = ops.persist_corrected_thought(
        org=None,
        session_uuid=SESSION_UUID,
        target_message_id="msg-1",
        original_sha256=sha,
        corrected_text=corrected,
    )
    assert result is not None
    row = graph_db_env.conn.execute(
        "SELECT content FROM thoughts WHERE id = ?",
        (result["thought_id"],),
    ).fetchone()
    assert row["content"] == corrected


# ── Idempotency contract ──────────────────────────────────────


def test_persist_is_idempotent_on_repeat_accept(graph_db_env):
    """Repeated accept handling never creates duplicate thoughts or edges."""
    src_id, _ = _seed_session_with_user_turn(graph_db_env)
    sha = _sha("Jason encoded")

    first = ops.persist_corrected_thought(
        org=None,
        session_uuid=SESSION_UUID,
        target_message_id="msg-1",
        original_sha256=sha,
        corrected_text="JSON encoded",
    )
    second = ops.persist_corrected_thought(
        org=None,
        session_uuid=SESSION_UUID,
        target_message_id="msg-1",
        original_sha256=sha,
        corrected_text="JSON encoded",
    )
    assert first is not None and second is not None
    assert first["thought_id"] == second["thought_id"]
    assert first["message_id"] == second["message_id"]
    assert first["created"] is True
    assert second["created"] is False

    thought_count = graph_db_env.conn.execute(
        "SELECT COUNT(*) FROM thoughts WHERE source_id = ?"
        " AND message_id = 'supersedes:msg-1'",
        (src_id,),
    ).fetchone()[0]
    assert thought_count == 1
    edge_count = graph_db_env.conn.execute(
        "SELECT COUNT(*) FROM edges WHERE relation = 'supersedes'",
    ).fetchone()[0]
    assert edge_count == 1


def test_persist_distinct_targets_create_distinct_thoughts(graph_db_env):
    """Two different accepted turns in the same source produce two thoughts."""
    src_id, _ = _seed_session_with_user_turn(
        graph_db_env, target_message_id="msg-1", raw_text="A",
    )
    second_thought = Thought(
        source_id=src_id, content="B", role="user", turn_number=2,
        message_id="msg-2",
    )
    graph_db_env.insert_thought(second_thought)
    graph_db_env.commit()

    ops.persist_corrected_thought(
        org=None, session_uuid=SESSION_UUID, target_message_id="msg-1",
        original_sha256=_sha("A"), corrected_text="A-fixed",
    )
    ops.persist_corrected_thought(
        org=None, session_uuid=SESSION_UUID, target_message_id="msg-2",
        original_sha256=_sha("B"), corrected_text="B-fixed",
    )

    rows = graph_db_env.conn.execute(
        "SELECT message_id, content FROM thoughts WHERE source_id = ?"
        " AND message_id LIKE 'supersedes:%' ORDER BY message_id",
        (src_id,),
    ).fetchall()
    assert [(r["message_id"], r["content"]) for r in rows] == [
        ("supersedes:msg-1", "A-fixed"),
        ("supersedes:msg-2", "B-fixed"),
    ]
    edge_count = graph_db_env.conn.execute(
        "SELECT COUNT(*) FROM edges WHERE relation = 'supersedes'",
    ).fetchone()[0]
    assert edge_count == 2


# ── Fail-closed contract ──────────────────────────────────────


def test_persist_returns_none_when_session_source_missing(graph_db_env):
    """No matching session source → no insert, no error."""
    sha = _sha("nope")
    result = ops.persist_corrected_thought(
        org=None,
        session_uuid="uuid-not-ingested",
        target_message_id="msg-1",
        original_sha256=sha,
        corrected_text="JSON encoded",
    )
    assert result is None
    assert graph_db_env.conn.execute(
        "SELECT COUNT(*) FROM thoughts"
    ).fetchone()[0] == 0
    assert graph_db_env.conn.execute(
        "SELECT COUNT(*) FROM edges"
    ).fetchone()[0] == 0


def test_persist_returns_none_when_original_thought_missing(graph_db_env):
    """Source exists, but no thought matches ``target_message_id`` → fail closed."""
    src = Source(
        type="session",
        platform="codex-cli",
        title="empty session",
        file_path="/tmp/empty.jsonl",
        metadata={"session_uuid": SESSION_UUID},
    )
    graph_db_env.insert_source(src)
    graph_db_env.commit()

    sha = _sha("text")
    result = ops.persist_corrected_thought(
        org=None,
        session_uuid=SESSION_UUID,
        target_message_id="msg-not-present",
        original_sha256=sha,
        corrected_text="JSON encoded",
    )
    assert result is None
    assert graph_db_env.conn.execute(
        "SELECT COUNT(*) FROM thoughts"
    ).fetchone()[0] == 0


def test_persist_rejects_empty_required_inputs(graph_db_env):
    """Defensive: missing required identity inputs short-circuit cleanly."""
    assert ops.persist_corrected_thought(
        org=None, session_uuid="", target_message_id="msg-1",
        original_sha256=_sha("x"), corrected_text="y",
    ) is None
    assert ops.persist_corrected_thought(
        org=None, session_uuid=SESSION_UUID, target_message_id="",
        original_sha256=_sha("x"), corrected_text="y",
    ) is None
    assert ops.persist_corrected_thought(
        org=None, session_uuid=SESSION_UUID, target_message_id="msg-1",
        original_sha256="", corrected_text="y",
    ) is None


# ── Codex live ↔ ingest message-id alignment ──────────────────


def test_codex_no_uuid_event_msg_resolves_via_shared_id_rule(
    graph_db_env, tmp_path,
):
    """Codex turn with no payload uuid: live + ingest agree on synthetic id.

    The bead's identity-resolution contract requires that an accepted
    correction whose live ``target_message_id`` was the synthetic
    ``codex-user:<sha1>`` resolves the *same* ingested thought. This test
    drives the alignment by ingesting a real Codex rollout JSONL whose
    ``event_msg.user_message`` row has no UUID, then calling persistence
    with the live-derived synthetic id.
    """
    raw_text = "Jason encoded"
    rollout = tmp_path / "rollout-2026-05-03T08-57-01-thread.jsonl"
    rollout.write_text("\n".join([
        json.dumps({
            "type": "session_meta",
            "timestamp": "2026-05-03T08:57:00Z",
            "payload": {
                "originator": "codex-tui", "model_provider": "openai",
                "cli_version": "0.147.0",
            },
        }),
        json.dumps({
            "type": "event_msg",
            "timestamp": "2026-05-03T08:57:01Z",
            "payload": {"type": "user_message", "message": raw_text},
        }),
    ]) + "\n")

    result = ingest_session_file(graph_db_env, rollout)
    assert result["status"] == "ingested"
    source_id = result["source_id"]

    # Pull the message_id ingest assigned and confirm it matches the live rule.
    from tools.dashboard.session_harness import codex_message_id
    live_id = codex_message_id(
        {"type": "user_message", "message": raw_text}, "user", raw_text,
    )
    assert live_id is not None
    assert live_id.startswith("codex-user:")

    row = graph_db_env.conn.execute(
        "SELECT message_id FROM thoughts WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    assert row["message_id"] == live_id

    # Accept-side persistence drives the same id and resolves the original.
    sha = _sha(raw_text)
    persisted = ops.persist_corrected_thought(
        org=None,
        session_uuid=rollout.stem,  # ingest uses file stem as session_uuid
        target_message_id=live_id,
        original_sha256=sha,
        corrected_text="JSON encoded",
    )
    assert persisted is not None
    assert persisted["created"] is True
    rows = graph_db_env.conn.execute(
        "SELECT content, message_id FROM thoughts WHERE source_id = ?"
        " ORDER BY turn_number",
        (source_id,),
    ).fetchall()
    assert [(r["content"], r["message_id"]) for r in rows] == [
        (raw_text, live_id),
        ("JSON encoded", f"supersedes:{live_id}"),
    ]


def test_claude_uuid_less_queue_resolves_via_shared_id_rule(
    graph_db_env, tmp_path,
):
    """Claude queue-operation identities align across viewer and graph ingest."""
    raw_text = "All of the images are already on Dr. hub."
    timestamp = "2026-07-23T19:04:29.105Z"
    session = tmp_path / "claude-queue-session.jsonl"
    payload = {
        "type": "queue-operation",
        "operation": "enqueue",
        "content": raw_text,
        "timestamp": timestamp,
    }
    session.write_text(json.dumps(payload) + "\n")

    result = ingest_session_file(graph_db_env, session)
    assert result["status"] == "ingested"
    source_id = result["source_id"]

    from tools.dashboard.session_harness import claude_queue_message_id

    live_id = claude_queue_message_id(payload, raw_text, timestamp)
    assert live_id is not None
    assert live_id.startswith("claude-queued-user:")
    row = graph_db_env.conn.execute(
        "SELECT message_id FROM thoughts WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    assert row["message_id"] == live_id

    persisted = ops.persist_corrected_thought(
        org=None,
        session_uuid=session.stem,
        target_message_id=live_id,
        original_sha256=_sha(raw_text),
        corrected_text="All of the images are already on Docker Hub.",
    )
    assert persisted is not None
    assert persisted["message_id"] == f"supersedes:{live_id}"


# ── Extra metadata pass-through ───────────────────────────────


def test_persist_attaches_extra_metadata_when_provided(graph_db_env):
    """Mode/reason/confidence flow into the thought row's metadata."""
    src_id, _ = _seed_session_with_user_turn(graph_db_env)
    sha = _sha("Jason encoded")
    ops.persist_corrected_thought(
        org=None,
        session_uuid=SESSION_UUID,
        target_message_id="msg-1",
        original_sha256=sha,
        corrected_text="JSON encoded",
        extra_metadata={
            "mode": "balanced",
            "reason": "dictation cleanup",
            "confidence": 0.9,
        },
    )
    row = graph_db_env.conn.execute(
        "SELECT metadata FROM thoughts WHERE source_id = ?"
        " AND message_id = 'supersedes:msg-1'",
        (src_id,),
    ).fetchone()
    meta = json.loads(row["metadata"])
    assert meta["mode"] == "balanced"
    assert meta["reason"] == "dictation cleanup"
    assert meta["confidence"] == pytest.approx(0.9)


def test_persist_extra_metadata_does_not_clobber_canonical_keys(graph_db_env):
    """Caller-supplied ``extra_metadata`` cannot override identity fields."""
    src_id, original_thought_id = _seed_session_with_user_turn(graph_db_env)
    sha = _sha("Jason encoded")
    ops.persist_corrected_thought(
        org=None,
        session_uuid=SESSION_UUID,
        target_message_id="msg-1",
        original_sha256=sha,
        corrected_text="JSON encoded",
        extra_metadata={
            "session_uuid": "imposter",
            "original_sha256": "imposter",
            "kind": "imposter",
        },
    )
    row = graph_db_env.conn.execute(
        "SELECT metadata FROM thoughts WHERE source_id = ?"
        " AND message_id = 'supersedes:msg-1'",
        (src_id,),
    ).fetchone()
    meta = json.loads(row["metadata"])
    assert meta["session_uuid"] == SESSION_UUID
    assert meta["original_sha256"] == sha
    assert meta["kind"] == "turn_correction_supersedes"
    assert meta["original_thought_id"] == original_thought_id
