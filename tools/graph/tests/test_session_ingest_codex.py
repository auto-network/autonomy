from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.graph import cli as graph_cli
from tools.graph.db import GraphDB
from tools.graph.ingest import (
    detect_session_format,
    ingest_all_claude_code,
    ingest_session_file,
)


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _session_meta(ts: str = "2026-04-24T05:00:00Z") -> dict:
    return {
        "type": "session_meta",
        "timestamp": ts,
        "payload": {
            "originator": "codex-tui",
            "model_provider": "openai",
        },
    }


def _user_message(text: str, ts: str) -> dict:
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {"type": "user_message", "message": text},
    }


def _agent_message(text: str, ts: str) -> dict:
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {"type": "agent_message", "message": text},
    }


def _token_count(input_tokens: int, output_tokens: int, ts: str) -> dict:
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
            },
        },
    }


def _exec_call(cmd: str, ts: str = "2026-04-24T05:00:03Z") -> dict:
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd}),
            "call_id": "call_exec_1",
        },
    }


@pytest.fixture
def graph_db(tmp_path) -> GraphDB:
    db = GraphDB(tmp_path / "graph.db")
    yield db
    db.close()


def test_detect_session_format_recognizes_codex_rollout(tmp_path):
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, [_session_meta()])

    assert detect_session_format(jsonl) == "codex"


def test_ingest_session_file_routes_codex_text_only(graph_db, tmp_path):
    jsonl = tmp_path / "rollout-2026-04-24T05-00-00-thread.jsonl"
    _write_jsonl(jsonl, [
        _session_meta(),
        _user_message(
            "Now your remaining commit for the session viewer resume code?",
            "2026-04-24T05:00:01Z",
        ),
        _exec_call("git status --short"),
        _user_message(
            '<crosstalk from="peer" label="peer" source="x">ignore me</crosstalk>',
            "2026-04-24T05:00:03Z",
        ),
        _agent_message(
            "Committed as c02b484 with the resume recovery fix.",
            "2026-04-24T05:00:04Z",
        ),
        _token_count(111, 22, "2026-04-24T05:00:05Z"),
    ])

    result = ingest_session_file(graph_db, jsonl)
    assert result["status"] == "ingested"
    assert result["thoughts"] == 1
    assert result["derivations"] == 1

    source = graph_db.conn.execute(
        "SELECT platform, metadata FROM sources WHERE id = ?",
        (result["source_id"],),
    ).fetchone()
    assert source["platform"] == "codex-cli"
    meta = json.loads(source["metadata"])
    assert meta["total_input_tokens"] == 111
    assert meta["total_output_tokens"] == 22

    thought_rows = graph_db.conn.execute(
        "SELECT role, content FROM thoughts WHERE source_id = ? ORDER BY turn_number",
        (result["source_id"],),
    ).fetchall()
    assert [(row["role"], row["content"]) for row in thought_rows] == [
        ("user", "Now your remaining commit for the session viewer resume code?"),
    ]

    derivation_rows = graph_db.conn.execute(
        "SELECT content FROM derivations WHERE source_id = ? ORDER BY turn_number",
        (result["source_id"],),
    ).fetchall()
    assert [row["content"] for row in derivation_rows] == [
        "Committed as c02b484 with the resume recovery fix.",
    ]

    search_rows = graph_db.search("remaining commit", include_raw=True)
    assert any(
        row.get("result_type") == "thought"
        and "remaining commit for the session viewer resume" in row.get("content", "")
        for row in search_rows
    )
    assert graph_db.search("git status", include_raw=True) == []


def test_attention_includes_codex_user_messages(graph_db, tmp_path):
    jsonl = tmp_path / "rollout-2026-04-24T05-21-34-thread.jsonl"
    _write_jsonl(jsonl, [
        _session_meta(),
        _user_message(
            "Can you fix the read marker so that it is created on the API path as well and make that its own commit.",
            "2026-04-24T05:21:34Z",
        ),
        _agent_message("Fixed and committed as fa94be8.", "2026-04-24T05:21:55Z"),
    ])

    ingest_session_file(graph_db, jsonl)
    rows = graph_cli._query_attention(
        graph_db,
        search="read marker so that it is created on the API path",
        last=20,
    )

    assert len(rows) == 1
    assert rows[0]["content"].startswith("Can you fix the read marker")


def test_cmd_ingest_session_routes_codex_rollout(graph_db, tmp_path, monkeypatch, capsys):
    jsonl = tmp_path / "rollout-2026-04-24T05-32-50-thread.jsonl"
    _write_jsonl(jsonl, [
        _session_meta(),
        _user_message(
            "Now your remaining commit for the session viewer resume code?",
            "2026-04-24T05:32:50Z",
        ),
    ])

    monkeypatch.setenv("GRAPH_DB", str(graph_db.db_path))
    args = SimpleNamespace(file=str(jsonl), project=None, db=graph_db.db_path)
    graph_cli.cmd_ingest_session(args)
    source_id = capsys.readouterr().out.strip()

    row = graph_db.conn.execute(
        "SELECT platform FROM sources WHERE id = ?",
        (source_id,),
    ).fetchone()
    assert row["platform"] == "codex-cli"


def test_ingest_all_claude_code_discovers_nested_codex_rollouts(
    graph_db, tmp_path, monkeypatch
):
    nested = (
        tmp_path
        / "data"
        / "agent-runs"
        / "auto-1"
        / "sessions"
        / "2026"
        / "04"
        / "24"
        / "rollout-2026-04-24T06-00-00-thread.jsonl"
    )
    _write_jsonl(nested, [
        _session_meta(),
        _user_message("Nested codex rollout should be discovered.", "2026-04-24T06:00:01Z"),
    ])

    monkeypatch.setattr("tools.graph.ingest._REPO_ROOT", tmp_path)
    monkeypatch.setattr("tools.graph.ingest.Path.home", lambda: tmp_path / "home")

    results = ingest_all_claude_code(graph_db)

    assert any(result.get("file") == str(nested) for result in results)
    row = graph_db.conn.execute(
        "SELECT COUNT(*) FROM thoughts WHERE content = ?",
        ("Nested codex rollout should be discovered.",),
    ).fetchone()
    assert row[0] == 1
