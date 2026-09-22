"""Grok Build (xAI) session harness contract tests.

Pins the provider-specific extraction boundary: ACP ``updates.jsonl``
records become the same normalized entries the shared monitor and viewer
consume. The fixture under ``fixtures/grok/`` is a real v1.0.40 transcript
(2026-09-22): one user turn, a ``run_terminal_command`` tool call with its
result, two assistant text segments and the ``turn_completed`` accounting.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from tools.dashboard.session_harness import (
    GROK_HARNESS,
    classify_grok_transcript,
    extract_grok_usage_delta,
    get_session_harness,
    is_grok_sidecar,
    is_grok_transcript,
    parse_grok_log_line,
    resolve_harness_for_path,
    resolve_harness_for_session_row,
    transcript_session_uuid,
)

FIXTURES = Path(__file__).parent / "fixtures" / "grok"
SESSION_ID = "01a0c7a2-98ab-70d1-9905-1dcb9489fc45"


def _fixture_lines() -> list[str]:
    return [l for l in (FIXTURES / "updates.jsonl").read_text().splitlines() if l.strip()]


def _update(session_update: str, *, tool_meta: dict | None = None, **fields) -> str:
    """One ``session/update`` line. ``tool_meta`` lands where Grok puts its
    ``x.ai/tool`` descriptor: on the UPDATE's ``_meta`` (the params-level
    ``_meta`` carries the event id and clock)."""
    update = {"sessionUpdate": session_update, **fields}
    if tool_meta is not None:
        update["_meta"] = {"x.ai/tool": tool_meta}
    return json.dumps({
        "timestamp": 1790055720,
        "method": "session/update",
        "params": {
            "sessionId": SESSION_ID,
            "update": update,
            "_meta": {"eventId": f"{SESSION_ID}-9", "agentTimestampMs": 1790055720257},
        },
    })


def _session_dir(tmp_path: Path, *, parent: str | None = None, with_summary: bool = True) -> Path:
    d = tmp_path / "sessions" / "%2Fworkspace%2Frepo" / SESSION_ID
    d.mkdir(parents=True)
    (d / "updates.jsonl").write_text((FIXTURES / "updates.jsonl").read_text())
    (d / "chat_history.jsonl").write_text('{"type": "system", "content": "wire"}\n')
    if with_summary:
        doc = json.loads((FIXTURES / "summary.json").read_text())
        if parent:
            doc["parent_session_id"] = parent
        (d / "summary.json").write_text(json.dumps(doc))
    return d


# ── registry / detection ───────────────────────────────────────────────


def test_grok_is_a_registered_harness():
    assert get_session_harness("grok") is GROK_HARNESS
    assert GROK_HARNESS.name == "grok"


def test_updates_jsonl_resolves_to_the_grok_harness(tmp_path):
    d = _session_dir(tmp_path)
    assert resolve_harness_for_path(d / "updates.jsonl").name == "grok"


def test_session_meta_harness_wins_for_grok(tmp_path):
    d = _session_dir(tmp_path)
    (tmp_path / "sessions" / ".session_meta.json").write_text(json.dumps({"harness": "grok"}))
    assert resolve_harness_for_path(d / "updates.jsonl").name == "grok"


def test_session_row_with_grok_harness_resolves():
    assert resolve_harness_for_session_row({"harness": "grok"}).name == "grok"


def test_sidecars_are_not_transcripts():
    for name in ("chat_history.jsonl", "events.jsonl", "rewind_points.jsonl", "feedback.jsonl", "prompt_history.jsonl"):
        assert is_grok_sidecar(Path("/x") / name)
        assert not is_grok_transcript(Path("/x") / name)
    assert is_grok_transcript(Path("/x/updates.jsonl"))
    assert not is_grok_sidecar(Path("/x/updates.jsonl"))
    assert not is_grok_sidecar(Path("/x/rollout-2026-01-01T00-00-00-abc.jsonl"))


def test_session_uuid_is_the_directory_name(tmp_path):
    d = _session_dir(tmp_path)
    assert transcript_session_uuid(d / "updates.jsonl") == SESSION_ID
    # Other harnesses keep the stem.
    assert transcript_session_uuid(Path("/x/abc-123.jsonl")) == "abc-123"


def test_classification_is_unknown_until_summary_exists(tmp_path):
    d = _session_dir(tmp_path, with_summary=False)
    assert classify_grok_transcript(d / "updates.jsonl") == ("unknown", "summary_missing")


def test_classification_main_and_subagent(tmp_path):
    d = _session_dir(tmp_path)
    assert classify_grok_transcript(d / "updates.jsonl")[0] == "main"
    d2 = _session_dir(tmp_path / "child", parent="00000000-0000-7000-8000-000000000000")
    assert classify_grok_transcript(d2 / "updates.jsonl") == ("subagent", "parent_session_id")


# ── parsing ────────────────────────────────────────────────────────────


def test_fixture_parses_into_the_shared_tile_taxonomy():
    entries: list[dict] = []
    for line in _fixture_lines():
        parsed = parse_grok_log_line(line)
        if parsed is None:
            continue
        entries.extend(parsed if isinstance(parsed, list) else [parsed])
    types = [e["type"] for e in entries]
    assert types == [
        "user", "assistant_text", "tool_use", "tool_result", "assistant_text", "grok_turn_complete",
    ]
    user = entries[0]
    assert user["content"].startswith("Reply with exactly: GROK-OK.")
    assert user["message_id"] == f"{SESSION_ID}-2"
    assert user["timestamp"] == "2026-09-22T05:41:57.099Z"
    tool_use = entries[2]
    assert tool_use["tool_name"] == "Bash"
    assert tool_use["native_tool_name"] == "run_terminal_command"
    assert tool_use["input"]["command"] == "echo hello-from-grok && uname -s"
    assert tool_use["tool_id"] == "call-07397641-4f6c-4263-ba49-81b2c78ff4d1-0"
    result = entries[3]
    assert result["tool_id"] == tool_use["tool_id"]
    assert result["content"] == "hello-from-grok\nLinux\n"
    assert result["is_error"] is False
    assert entries[5]["internal"] is True


def test_in_progress_tool_update_renders_nothing():
    line = _update(
        "tool_call_update", toolCallId="call-1", kind="execute",
        title="Execute `ls`", rawInput={"command": "ls"},
    )
    assert parse_grok_log_line(line) is None


def test_failed_tool_update_is_an_error_result():
    line = _update(
        "tool_call_update", toolCallId="call-1", status="failed",
        content=[{"type": "content", "content": {"type": "text", "text": "boom"}}],
    )
    entry = parse_grok_log_line(line)
    assert entry["type"] == "tool_result" and entry["is_error"] is True
    assert entry["content"] == "boom"


def test_read_file_maps_to_read_tile_with_file_path():
    line = _update(
        "tool_call", toolCallId="call-2", title="read_file",
        rawInput={"target_file": "README.md", "offset": 1, "limit": 40},
        tool_meta={"name": "read_file", "kind": "read"},
    )
    entry = parse_grok_log_line(line)
    assert entry["tool_name"] == "Read"
    assert entry["input"] == {"file_path": "README.md", "offset": 1, "limit": 40}
    assert entry["tool_kind"] == "read"


def test_unknown_tool_keeps_its_grok_name():
    line = _update(
        "tool_call", toolCallId="call-3", title="scheduler_list", rawInput={},
        tool_meta={"name": "scheduler_list", "kind": "other"},
    )
    entry = parse_grok_log_line(line)
    assert entry["tool_name"] == "scheduler_list"


def test_thought_chunk_is_thinking():
    line = _update("agent_thought_chunk", content={"type": "text", "text": "Considering the repo layout."})
    assert parse_grok_log_line(line)["type"] == "thinking"


def test_plan_update_is_a_todo_plan():
    line = _update("plan", entries=[
        {"content": "Read the launcher", "status": "completed", "priority": "high"},
        {"content": "Patch the parser", "status": "in_progress", "priority": "medium"},
    ])
    entry = parse_grok_log_line(line)
    assert entry["type"] == "todo_plan"
    assert [t["subject"] for t in entry["todos"]] == ["Read the launcher", "Patch the parser"]
    assert entry["todos"][1]["status"] == "in_progress"


def test_todo_write_tool_call_also_emits_a_plan():
    line = _update(
        "tool_call", toolCallId="call-4", title="todo_write",
        rawInput={"merge": False, "todos": [{"content": "Ship it", "status": "pending"}]},
        tool_meta={"name": "todo_write", "kind": "other"},
    )
    entries = parse_grok_log_line(line)
    assert [e["type"] for e in entries] == ["tool_use", "todo_plan"]
    assert entries[0]["tool_name"] == "TodoWrite"


def test_inbound_crosstalk_user_message_is_a_crosstalk_tile():
    body = (
        '<crosstalk from="auto-0922-000000" label="peer" source="abc" turn="3" '
        'harness="claude" model="" timestamp="2026-09-22T00:00:00Z">\n'
        "please review the diff\n</crosstalk>"
    )
    entry = parse_grok_log_line(_update("user_message_chunk", content={"type": "text", "text": body}))
    assert entry["type"] == "crosstalk"
    assert entry["sender"] == "auto-0922-000000"
    assert entry["content"] == "please review the diff"


def test_non_update_lines_are_ignored():
    assert parse_grok_log_line(json.dumps({"method": "session/other", "params": {}})) is None
    assert parse_grok_log_line("not json") is None


# ── extractors ─────────────────────────────────────────────────────────


def test_usage_delta_subtracts_cache_from_full_input():
    turn = json.loads(_fixture_lines()[-1])
    delta = extract_grok_usage_delta(turn)
    assert delta == {
        "usage_input_tokens": 27557 - 13824,
        "usage_cache_creation_tokens": 0,
        "usage_cache_read_tokens": 13824,
        "usage_output_tokens": 209,
    }
    assert GROK_HARNESS.extract_usage_delta(json.loads(_fixture_lines()[0])) is None


def test_model_context_and_preview_extraction():
    model = None
    ctx = 0
    previews = []
    for line in _fixture_lines():
        raw = json.loads(line)
        model = GROK_HARNESS.extract_model(raw, model)
        ctx = GROK_HARNESS.extract_context_tokens(raw, ctx)
        text = GROK_HARNESS.extract_message_text(raw)
        if text:
            previews.append(text)
    assert model == "x-ai/grok-4.6"
    assert ctx == 13872
    assert previews[0].startswith("Reply with exactly: GROK-OK.")
    assert previews[-1].startswith("GROK-OK.")


def test_harness_state_tracks_last_user_message():
    raw = json.loads(_fixture_lines()[0])
    state = GROK_HARNESS.extract_harness_state(raw, {})
    assert state == {"last_user_message_at": "2026-09-22T05:41:57.099Z"}
    assert GROK_HARNESS.extract_harness_state(raw, state) is state


# ── registration / linking ────────────────────────────────────────────


def test_grok_register_session_uses_run_sessions_dir(tmp_path):
    run_dir = tmp_path / "run"
    sessions_dir = run_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    monitor = AsyncMock()
    asyncio.run(GROK_HARNESS.register_session(
        monitor=monitor, tmux_name="auto-grok-1", session_type="container",
        project="autonomy-grok", run_dir=run_dir, seed_message="Starting...",
    ))
    kwargs = monitor.register.await_args.kwargs
    assert kwargs["jsonl_path"] == sessions_dir
    assert kwargs["resolution_dir"] == sessions_dir
    assert kwargs["harness"] == "grok"


def test_resolve_session_refuses_sidecars(tmp_path):
    d = _session_dir(tmp_path)
    assert GROK_HARNESS.resolve_session(tmux_name="auto-grok-1", jsonl_path=d / "chat_history.jsonl") is None
