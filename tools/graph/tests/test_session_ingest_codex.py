from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.graph import cli as graph_cli
from tools.graph import ops as graph_ops
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
            "cli_version": "0.146.0",
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
    # W1 §12.3: the crosstalk-noise user_message is no longer dropped — it's
    # ingested with role='injected' so it stays searchable, so thoughts now
    # counts both the real user turn and the injected one.
    assert result["thoughts"] == 2
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
        ("injected", '<crosstalk from="peer" label="peer" source="x">ignore me</crosstalk>'),
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
    rows = graph_ops._query_attention(
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

    # ingest_all_claude_code scans "<DATA_ROOT>/agent-runs" (ingest.py) since the
    # one-DATA_ROOT refactor (auto-dnjn0); _REPO_ROOT no longer drives that scan.
    # Point DATA_ROOT at the tmp tree where this test writes the nested rollout.
    monkeypatch.setattr("tools.graph.ingest.DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr("tools.graph.ingest._REPO_ROOT", tmp_path)
    monkeypatch.setattr("tools.graph.ingest.Path.home", lambda: tmp_path / "home")

    results = ingest_all_claude_code(graph_db)

    assert any(result.get("file") == str(nested) for result in results)
    row = graph_db.conn.execute(
        "SELECT COUNT(*) FROM thoughts WHERE content = ?",
        ("Nested codex rollout should be discovered.",),
    ).fetchone()
    assert row[0] == 1


# ══════════════════════════════════════════════════════════════════════
# auto-cpg1x — codex renumbering dedup regression
# ══════════════════════════════════════════════════════════════════════
#
# W1's role='injected' change made previously-dropped noise user_message
# entries start consuming turn-number slots. A codex source ingested
# BEFORE that change (numbering skipped noise) that grows AFTER it
# re-parses with every subsequent turn shifted to a higher number — the
# incremental cursor was purely positional (turn_number > max_turn), so
# it read the shifted positions as new content and duplicated
# already-ingested turns (confirmed prod damage: source e26143b6-a08
# duplicated turns 76-84 at 22:37Z). Fixed by message-id-based dedup on
# top of the positional filter (_dedup_new_turns).
#
# Repro per the bead: ingest a file version with the noise-triggering
# line's effect absent (simulating pre-W1 numbering, since the noise line
# was always physically in the transcript but never consumed a slot),
# then re-ingest the grown file at current code — assert zero duplicate
# message_ids and the genuinely new content lands.


class TestCodexRenumberingDedupRegression:
    def test_growth_reingest_after_renumbering_produces_no_duplicates(self, graph_db, tmp_path):
        jsonl = tmp_path / "rollout-2026-05-01T09-00-00-thread.jsonl"

        # "Pre-W1" state: same two real turns a pre-W1 ingest would have
        # numbered 1 and 2 (the noise line was always in the raw
        # transcript but consumed no turn-number slot back then — this
        # fixture just omits it, which is numerically equivalent).
        _write_jsonl(jsonl, [
            _session_meta(),
            _user_message("Ship the fix for the offset bug", "2026-05-01T09:00:01Z"),
            _agent_message("Done — committed as abc123.", "2026-05-01T09:00:02Z"),
        ])
        r1 = ingest_session_file(graph_db, jsonl)
        assert r1["status"] == "ingested"
        source_id = r1["source_id"]
        assert graph_db.get_max_turn(source_id) == 2

        # "Post-W1 growth": the file now includes the noise line (which
        # W1 ingests as role='injected', consuming turn 2 and pushing the
        # real assistant turn to 3) PLUS one genuinely new turn.
        _write_jsonl(jsonl, [
            _session_meta(),
            _user_message("Ship the fix for the offset bug", "2026-05-01T09:00:01Z"),
            _user_message('<crosstalk from="peer">ignore me</crosstalk>', "2026-05-01T09:00:01Z"),
            _agent_message("Done — committed as abc123.", "2026-05-01T09:00:02Z"),
            _user_message("One more thing before we wrap up", "2026-05-01T09:05:00Z"),
        ])
        r2 = ingest_session_file(graph_db, jsonl)
        assert r2["status"] == "updated"
        assert r2["source_id"] == source_id

        thought_rows = graph_db.conn.execute(
            "SELECT content, role, message_id FROM thoughts WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        deriv_rows = graph_db.conn.execute(
            "SELECT content, message_id FROM derivations WHERE source_id = ?",
            (source_id,),
        ).fetchall()

        all_message_ids = [r["message_id"] for r in list(thought_rows) + list(deriv_rows) if r["message_id"]]
        assert len(all_message_ids) == len(set(all_message_ids)), (
            f"duplicate message_id(s) found: {all_message_ids}"
        )

        contents = [r["content"] for r in thought_rows]
        assert contents.count("Ship the fix for the offset bug") == 1, "real turn must not be duplicated"
        assert "One more thing before we wrap up" in contents, "genuinely new content must still land"
        assert [r["content"] for r in deriv_rows].count("Done — committed as abc123.") == 1
        # Known residual of the two-stage filter (coarse turn_number >
        # max_turn, then message-id dedup): the injected turn renumbered
        # INTO the already-consumed turn_number<=max_turn range (here,
        # slot 2 — previously the real assistant turn's slot) never clears
        # the coarse filter, so it isn't backfilled by an incremental
        # pass. The hotfix's job is stopping duplication, not retroactively
        # recovering every historically-skipped position — a force
        # re-ingest (full reparse) would pick it up. Documented here so
        # it isn't mysterious; not a regression this bead is scoped to fix.
        assert contents.count('<crosstalk from="peer">ignore me</crosstalk>') == 0

    def test_reingest_with_no_growth_still_dedupes(self, graph_db, tmp_path):
        """Even with nothing new appended, re-running current code against
        an old-numbered source must not duplicate the renumbered tail."""
        jsonl = tmp_path / "rollout-2026-05-01T09-10-00-thread.jsonl"
        _write_jsonl(jsonl, [
            _session_meta(),
            _user_message("Old turn one", "2026-05-01T09:10:01Z"),
            _agent_message("Old turn two", "2026-05-01T09:10:02Z"),
        ])
        r1 = ingest_session_file(graph_db, jsonl)
        source_id = r1["source_id"]

        _write_jsonl(jsonl, [
            _session_meta(),
            _user_message("Old turn one", "2026-05-01T09:10:01Z"),
            _user_message('<crosstalk from="peer">noise</crosstalk>', "2026-05-01T09:10:01Z"),
            _agent_message("Old turn two", "2026-05-01T09:10:02Z"),
        ])
        r2 = ingest_session_file(graph_db, jsonl)
        assert r2["status"] in ("updated", "refreshed")

        deriv_rows = graph_db.conn.execute(
            "SELECT content FROM derivations WHERE source_id = ?", (source_id,),
        ).fetchall()
        assert [r["content"] for r in deriv_rows].count("Old turn two") == 1
