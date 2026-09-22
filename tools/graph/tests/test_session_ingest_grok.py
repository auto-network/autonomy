"""Grok Build (xAI) ``updates.jsonl`` ingest: detection, text-only routing,
turn shape, token totals, and the split-invariance the tail appender needs.

The corpus mirrors a real v1.0.40 transcript (2026-09-22): user turn, tool
call + result, two assistant text segments, ``turn_completed`` accounting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.graph.appender import extractor_class_for_harness
from tools.graph.db import GraphDB
from tools.graph.ingest import (
    GrokTurnExtractor,
    detect_session_format,
    ingest_session_file,
    parse_grok_session,
)

SID = "01a0c7a2-98ab-70d1-9905-1dcb9489fc45"


def _upd(kind: str, ts_ms: int, event: int, **fields) -> dict:
    method = "_x.ai/session/update" if kind == "turn_completed" else "session/update"
    return {
        "timestamp": ts_ms // 1000,
        "method": method,
        "params": {
            "sessionId": SID,
            "update": {"sessionUpdate": kind, **fields},
            "_meta": {"eventId": f"{SID}-{event}", "agentTimestampMs": ts_ms},
        },
    }


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _corpus() -> list[dict]:
    return [
        _upd("user_message_chunk", 1790055717099, 2, content=_text(
            "Reply with exactly: GROK-OK and run uname."), _meta={"modelId": "x-ai/grok-4.6", "promptIndex": 0}),
        _upd("agent_message_chunk", 1790055719708, 18, content=_text(
            "I'll run that shell command first.")),
        _upd("tool_call", 1790055720257, 20, toolCallId="call-1", title="run_terminal_command",
             rawInput={"command": "uname -s"}, _meta={"x.ai/tool": {"name": "run_terminal_command", "kind": "execute"}}),
        _upd("tool_call_update", 1790055720266, 24, toolCallId="call-1", status="completed",
             content=[{"type": "content", "content": _text("Linux\n")}],
             rawOutput={"type": "Bash", "output_for_prompt": "exit: 0\nLinux\n", "exit_code": 0}),
        _upd("agent_message_chunk", 1790055721961, 54, content=_text("GROK-OK. Kernel: Linux.")),
        _upd("user_message_chunk", 1790055730000, 60, content=_text(
            '<crosstalk from="peer" label="peer" source="x">ignore me</crosstalk>')),
        _upd("turn_completed", 1790055722041, 58, prompt_id="p1", stop_reason="end_turn",
             usage={"inputTokens": 27557, "outputTokens": 209, "totalTokens": 27766,
                    "cachedReadTokens": 13824, "cacheCreationTokens": 0, "reasoningTokens": 131,
                    "modelCalls": 2, "modelUsage": {"x-ai/grok-4.6": {"inputTokens": 27557, "outputTokens": 209}}}),
    ]


def _write_session(root: Path, entries: list[dict], *, sid: str = SID) -> Path:
    d = root / "sessions" / "%2Fworkspace%2Frepo" / sid
    d.mkdir(parents=True, exist_ok=True)
    path = d / "updates.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


@pytest.fixture
def graph_db(tmp_path) -> GraphDB:
    db = GraphDB(tmp_path / "graph.db")
    yield db
    db.close()


def test_detect_session_format_recognizes_updates_jsonl(tmp_path):
    path = _write_session(tmp_path, _corpus())
    assert detect_session_format(path) == "grok"


def test_detect_session_format_honours_session_meta_harness(tmp_path):
    path = _write_session(tmp_path, _corpus())
    (tmp_path / "sessions" / ".session_meta.json").write_text(json.dumps({"harness": "grok"}))
    assert detect_session_format(path) == "grok"


def test_parse_grok_session_keeps_chat_only():
    pass  # covered by the ingest test below; kept as a named intent


def test_ingest_session_file_routes_grok_text_only(graph_db, tmp_path):
    path = _write_session(tmp_path, _corpus())
    result = ingest_session_file(graph_db, path)
    assert result["status"] == "ingested"
    # user turn + injected crosstalk brief; the two assistant segments are derivations
    assert result["thoughts"] == 2
    assert result["derivations"] == 2

    source = graph_db.conn.execute(
        "SELECT platform, metadata FROM sources WHERE id = ?", (result["source_id"],),
    ).fetchone()
    assert source["platform"] == "grok-build"
    meta = json.loads(source["metadata"])
    assert meta["total_input_tokens"] == 27557
    assert meta["total_output_tokens"] == 209

    thought_rows = graph_db.conn.execute(
        "SELECT role, content, message_id FROM thoughts WHERE source_id = ? ORDER BY turn_number",
        (result["source_id"],),
    ).fetchall()
    assert [(r["role"], r["content"]) for r in thought_rows] == [
        ("user", "Reply with exactly: GROK-OK and run uname."),
        ("injected", '<crosstalk from="peer" label="peer" source="x">ignore me</crosstalk>'),
    ]
    assert thought_rows[0]["message_id"] == f"{SID}-2"

    derivation_rows = graph_db.conn.execute(
        "SELECT content FROM derivations WHERE source_id = ? ORDER BY turn_number",
        (result["source_id"],),
    ).fetchall()
    assert [r["content"] for r in derivation_rows] == [
        "I'll run that shell command first.",
        "GROK-OK. Kernel: Linux.",
    ]
    # Tool output never reaches content ingest.
    assert graph_db.search("uname", include_raw=True) != []   # the user asked for it
    assert all("exit: 0" not in (row.get("content") or "") for row in graph_db.search("Linux", include_raw=True))


def test_parse_grok_session_meta_uses_directory_uuid_and_model(tmp_path):
    path = _write_session(tmp_path, _corpus())
    meta, turns = parse_grok_session(path)
    assert meta["session_id"] == SID
    assert meta["platform"] == "grok-build"
    assert meta["model"] == "x-ai/grok-4.6"
    assert meta["turn_count"] == 4
    assert meta["started_at"] == "2026-09-22T05:41:57.099Z"
    assert [t["role"] for t in turns] == ["user", "assistant", "assistant", "injected"]


def test_grok_extractor_is_split_invariant():
    """Feeding part A, saving state, resuming with part B must equal one pass."""
    corpus = _corpus()
    whole = GrokTurnExtractor()
    expected = [t for e in corpus if (t := whole.feed(e)) is not None]
    for split in range(len(corpus) + 1):
        a = GrokTurnExtractor()
        got = [t for e in corpus[:split] if (t := a.feed(e)) is not None]
        b = GrokTurnExtractor.from_state(json.loads(json.dumps(a.state)))
        got += [t for e in corpus[split:] if (t := b.feed(e)) is not None]
        assert got == expected, f"split at {split}"
        assert b.state == whole.state


def test_appender_selects_the_grok_extractor():
    assert extractor_class_for_harness("grok") is GrokTurnExtractor


def test_short_or_empty_chunks_are_not_turns():
    ex = GrokTurnExtractor()
    assert ex.feed(_upd("agent_message_chunk", 1, 1, content=_text("ok"))) is None
    assert ex.feed(_upd("agent_message_chunk", 2, 2, content=_text(""))) is None
    assert ex.feed({"method": "session/other", "params": {}}) is None
    assert ex.state["turn_number"] == 0
