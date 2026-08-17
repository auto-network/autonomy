from __future__ import annotations

import asyncio
import json
from pathlib import Path


class _FakeHarness:
    name = "fake"

    def parse_line(self, line: str, ctx=None):
        return json.loads(line)


def test_build_session_context_keeps_conversation_and_compacts_tool_payloads(tmp_path, monkeypatch):
    from tools.dashboard import voiceover

    monkeypatch.setattr(
        voiceover,
        "resolve_harness_for_path",
        lambda _path: _FakeHarness(),
    )
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        json.dumps({"type": "message", "role": "user", "content": "Please fix the voice path."}),
        json.dumps({"type": "tool_result", "role": "tool", "result_kind": "exec_command", "content": "SECRET " * 500}),
        json.dumps({"type": "message", "role": "assistant", "content": "The implementation is ready for tests."}),
    ]))
    row = {
        "tmux_name": "auto-test",
        "label": "Voice path",
        "harness": "codex",
        "role": "builder",
        "topics": json.dumps(["Running regressions"]),
    }

    context = voiceover.build_session_context(row, transcript)

    assert "Session: Voice path" in context
    assert "Current status: Running regressions" in context
    assert "OPERATOR: Please fix the voice path." in context
    assert "TOOL: exec_command completed" in context
    assert "SECRET" not in context
    assert "SESSION: The implementation is ready for tests." in context


def test_build_session_context_seeds_current_codex_version_outside_bounded_tail(tmp_path):
    from tools.dashboard import voiceover

    transcript = tmp_path / "rollout-current.jsonl"
    lines = [
        {"type": "session_meta", "payload": {
            "originator": "codex-tui", "cli_version": "0.148.0"}},
        {"type": "event_msg", "payload": {
            "type": "reasoning", "text": "x" * (voiceover.MAX_TRANSCRIPT_BYTES + 100)}},
        {"type": "response_item", "timestamp": "2026-08-17T00:00:00Z", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "What changed?"}]}},
        {"type": "response_item", "timestamp": "2026-08-17T00:00:01Z", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "The parser now fails closed."}]}},
    ]
    transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    context = voiceover.build_session_context(
        {"tmux_name": "auto-current", "harness": "codex"}, transcript)

    assert "OPERATOR: What changed?" in context
    assert "SESSION: The parser now fails closed." in context


def test_ask_session_builds_spoken_first_prompt_and_bounded_history(tmp_path, monkeypatch):
    from tools.dashboard import voiceover

    monkeypatch.setattr(voiceover, "build_session_context", lambda _row, _path: "SESSION: Tests are passing.")
    captured = {}

    def fake_chat(messages, model):
        captured["messages"] = messages
        captured["model"] = model
        return "No intervention is needed. The session is finishing its regression pass."

    monkeypatch.setattr(voiceover, "_ollama_chat_sync", fake_chat)
    path = tmp_path / "session.jsonl"
    path.write_text("")
    history = [{"role": "user", "content": f"question {i}"} for i in range(12)]

    answer = asyncio.run(voiceover.ask_session(
        session_id="auto-test",
        question="Do you need me?",
        row={"tmux_name": "auto-test"},
        path=path,
        history=history,
    ))

    assert answer.session_id == "auto-test"
    assert answer.text.startswith("No intervention")
    assert "output only natural speech" in captured["messages"][0]["content"]
    assert captured["messages"][-1] == {"role": "user", "content": "Do you need me?"}
    assert len(captured["messages"]) == 10  # system + eight bounded history messages + question


def test_clean_spoken_reply_removes_wrapper_and_caps_length():
    from tools.dashboard import voiceover

    assert voiceover._clean_spoken_reply("```text\nVoiceover: It is done.\n```") == "It is done."
    cleaned = voiceover._clean_spoken_reply("word " * 1_000)
    assert len(cleaned) <= voiceover.MAX_REPLY_CHARS + 1
    assert cleaned.endswith(".")


def test_voiceover_route_returns_local_answer(test_client, monkeypatch, tmp_path):
    from tools.dashboard import feature_flags, server, voiceover

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n")
    row = {"tmux_name": "auto-test", "jsonl_path": str(transcript), "harness": "codex"}
    monkeypatch.setattr(server.session_monitor, "get_one", lambda session_id: row if session_id == "auto-test" else None)
    monkeypatch.setattr(server.session_monitor, "resolve_session_file", lambda session_id: transcript if session_id == "auto-test" else None)
    monkeypatch.setattr(feature_flags, "is_enabled", lambda name: name == "voice.voiceover_enabled")

    async def fake_ask_session(**kwargs):
        assert kwargs["session_id"] == "auto-test"
        assert kwargs["question"] == "What changed?"
        return voiceover.VoiceoverAnswer("It added a read-only spoken path.", "llama-test", "auto-test")

    monkeypatch.setattr(voiceover, "ask_session", fake_ask_session)
    response = test_client.post("/api/voiceover/ask", json={
        "session_id": "auto-test",
        "question": "What changed?",
    })

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "text": "It added a read-only spoken path.",
        "model": "llama-test",
        "session_id": "auto-test",
    }


def test_voiceover_route_rejects_unknown_session(test_client, monkeypatch):
    from tools.dashboard import feature_flags, server

    monkeypatch.setattr(feature_flags, "is_enabled", lambda name: name == "voice.voiceover_enabled")
    monkeypatch.setattr(server.session_monitor, "get_one", lambda _session_id: None)
    monkeypatch.setattr(server.session_monitor, "_find_session_by_uuid", lambda _session_id: None)
    monkeypatch.setattr(server.session_monitor, "resolve_session_file", lambda _session_id: None)
    response = test_client.post("/api/voiceover/ask", json={
        "session_id": "missing",
        "question": "What is happening?",
    })

    assert response.status_code == 404
    assert response.json()["code"] == "session_not_found"


def test_voiceover_route_is_absent_when_feature_is_disabled(test_client, monkeypatch):
    from tools.dashboard import feature_flags

    monkeypatch.setattr(feature_flags, "is_enabled", lambda _name: False)
    response = test_client.post("/api/voiceover/ask", json={
        "session_id": "auto-test",
        "question": "What is happening?",
    })

    assert response.status_code == 404
    assert response.json()["code"] == "voiceover_disabled"
