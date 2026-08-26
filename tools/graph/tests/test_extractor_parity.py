"""Golden-corpus + property tests for the W1 incremental extractors (auto-9o9c5).

``ClaudeTurnExtractor`` / ``CodexTurnExtractor`` replace the old monolithic
``parse_claude_code_session`` / ``parse_codex_session`` loops with a
``feed(entry) -> turn | None`` state machine so the future tail appender
(W3) can resume mid-file instead of re-parsing from byte 0 every tick.

Two things must hold for that resume path to be trustworthy:

1. **Golden parity** — a batch feed of a realistic corpus (compaction,
   queue-ops, tool noise, sidechain, codex noise prefixes, token_count,
   uuid-less turns) produces the exact turn stream the pre-refactor
   parsers produced (AC-parity, §9.4).
2. **Split-invariance** — for *every* line-boundary split point,
   feeding part A, saving state, resuming with part B produces the same
   turns as feeding the whole thing at once. This is the property the
   tail appender's correctness depends on.
"""

from __future__ import annotations

import json

import pytest

from tools.graph.ingest import (
    ClaudeTurnExtractor,
    CodexTurnExtractor,
)


# ══════════════════════════════════════════════════════════════════════
# Claude corpus — one entry of every kind the extractor branches on
# ══════════════════════════════════════════════════════════════════════


def _c_user(text: str, ts: str, uuid: str = "u1") -> dict:
    return {
        "type": "user", "uuid": uuid,
        "message": {"role": "user", "content": text},
        "timestamp": ts,
    }


def _c_assistant_text(text: str, ts: str, uuid: str = "a1", model: str = "claude-test") -> dict:
    return {
        "type": "assistant", "uuid": uuid,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": model,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
        "timestamp": ts,
    }


def _c_assistant_tool_only(ts: str, uuid: str = "a-tool") -> dict:
    return {
        "type": "assistant", "uuid": uuid,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
        },
        "timestamp": ts,
    }


def _c_sidechain(ts: str) -> dict:
    return {
        "type": "user", "uuid": "side-1", "isSidechain": True,
        "message": {"role": "user", "content": "subagent chatter"},
        "timestamp": ts,
    }


def _c_is_meta(ts: str) -> dict:
    return {
        "type": "user", "uuid": "meta-1", "isMeta": True,
        "message": {"role": "user", "content": "local-command-stdout noise"},
        "timestamp": ts,
    }


def _c_queue_op(text: str, ts: str, uuid: str = "q1") -> dict:
    return {"type": "queue-operation", "uuid": uuid, "content": text, "timestamp": ts}


def _c_queue_op_notification(ts: str) -> dict:
    return {
        "type": "queue-operation", "uuid": "q-notif",
        "content": "<task-notification>ignore</task-notification>",
        "timestamp": ts,
    }


def _c_compact_meta(ts: str) -> dict:
    return {
        "type": "system", "uuid": "cmeta-1",
        "compactMetadata": {"turnsCompacted": 240, "preCompactionTokens": 198000},
        "timestamp": ts,
    }


def _c_compact_summary(body: str, ts: str) -> dict:
    return {
        "type": "user", "uuid": "csum-1", "isCompactSummary": True,
        "message": {"role": "user", "content": body},
        "timestamp": ts,
    }


def _c_low_signal(ts: str) -> dict:
    return {
        "type": "user", "uuid": "low-1",
        "message": {"role": "user", "content": "ok"},
        "timestamp": ts,
    }


CLAUDE_CORPUS = [
    _c_user("What does this function do?", "2026-05-01T10:00:00Z"),
    _c_assistant_tool_only("2026-05-01T10:00:01Z"),
    _c_assistant_text("It parses the config file.", "2026-05-01T10:00:02Z"),
    _c_sidechain("2026-05-01T10:00:03Z"),
    _c_is_meta("2026-05-01T10:00:04Z"),
    _c_queue_op("Also check the tests please", "2026-05-01T10:00:05Z"),
    _c_queue_op_notification("2026-05-01T10:00:06Z"),
    _c_low_signal("2026-05-01T10:00:07Z"),
    _c_compact_meta("2026-05-01T10:00:08Z"),
    _c_compact_summary(
        "This session is being continued from a previous conversation "
        "that ran out of context. Built the parser, wired the tests.",
        "2026-05-01T10:00:09Z",
    ),
    _c_user("Keep going please", "2026-05-01T10:00:10Z"),
]


def _feed_all(extractor, entries) -> list[dict]:
    out = []
    for e in entries:
        t = extractor.feed(e)
        if t is not None:
            out.append(t)
    return out


class TestClaudeExtractorGoldenCorpus:
    def test_batch_parse_matches_expected_turn_stream(self):
        turns = _feed_all(ClaudeTurnExtractor(), CLAUDE_CORPUS)
        stream = [(t["role"], t["content"], t.get("queued", False)) for t in turns]
        assert stream == [
            ("user", "What does this function do?", False),
            ("assistant", "It parses the config file.", False),
            ("user", "Also check the tests please", True),
            ("compact_summary",
             "This session is being continued from a previous conversation "
             "that ran out of context. Built the parser, wired the tests.",
             False),
            ("user", "Keep going please", False),
        ], "extractor dropped or reordered a turn the old parser would have kept"

    def test_turn_numbers_are_contiguous(self):
        turns = _feed_all(ClaudeTurnExtractor(), CLAUDE_CORPUS)
        assert [t["turn_number"] for t in turns] == list(range(1, len(turns) + 1))

    def test_compact_metadata_rides_with_the_summary_turn(self):
        turns = _feed_all(ClaudeTurnExtractor(), CLAUDE_CORPUS)
        summary = next(t for t in turns if t["role"] == "compact_summary")
        assert summary["compact_metadata"]["turnsCompacted"] == 240

    def test_final_state_totals(self):
        ext = ClaudeTurnExtractor()
        _feed_all(ext, CLAUDE_CORPUS)
        s = ext.state
        assert s["total_input_tokens"] == 10
        assert s["total_output_tokens"] == 20
        assert s["model"] == "claude-test"
        assert s["first_ts"] == "2026-05-01T10:00:00Z"
        assert s["last_ts"] == "2026-05-01T10:00:10Z"
        assert s["pending_compact_meta"] is None  # consumed by the summary turn


class TestClaudeExtractorSplitInvariance:
    """Property: for every line-boundary split, resume(state) + feed(b)
    produces the same turns as one continuous feed."""

    def test_every_split_point_matches_full_parse(self):
        full_turns = _feed_all(ClaudeTurnExtractor(), CLAUDE_CORPUS)
        for i in range(len(CLAUDE_CORPUS) + 1):
            a, b = CLAUDE_CORPUS[:i], CLAUDE_CORPUS[i:]
            ext_a = ClaudeTurnExtractor()
            turns_a = _feed_all(ext_a, a)
            ext_b = ClaudeTurnExtractor.from_state(ext_a.state)
            turns_b = _feed_all(ext_b, b)
            assert turns_a + turns_b == full_turns, f"split at line {i} diverged"

    def test_split_between_compact_meta_and_summary(self):
        """The trickiest split: pending_compact_meta must survive a resume
        boundary that falls exactly between the system entry and the
        isCompactSummary entry it belongs to."""
        split_idx = CLAUDE_CORPUS.index(
            next(e for e in CLAUDE_CORPUS if e.get("compactMetadata"))
        ) + 1
        a, b = CLAUDE_CORPUS[:split_idx], CLAUDE_CORPUS[split_idx:]

        ext_a = ClaudeTurnExtractor()
        _feed_all(ext_a, a)
        assert ext_a.state["pending_compact_meta"] is not None

        ext_b = ClaudeTurnExtractor.from_state(ext_a.state)
        turns_b = _feed_all(ext_b, b)
        summary = next(t for t in turns_b if t["role"] == "compact_summary")
        assert summary["compact_metadata"]["turnsCompacted"] == 240


# ══════════════════════════════════════════════════════════════════════
# Codex corpus
# ══════════════════════════════════════════════════════════════════════


def _x_session_meta(ts: str = "2026-05-01T09:00:00Z") -> dict:
    return {
        "type": "session_meta", "timestamp": ts,
        "payload": {
            "originator": "codex-tui",
            "model_provider": "openai",
            "model": "gpt-test",
            "cli_version": "0.146.0",
        },
    }


def _x_user(text: str, ts: str) -> dict:
    return {
        "type": "response_item", "timestamp": ts,
        "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": text}]},
    }


def _x_agent(text: str, ts: str) -> dict:
    return {
        "type": "response_item", "timestamp": ts,
        "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": text}]},
    }


def _x_event_msg_twin(text: str, role: str, ts: str) -> dict:
    """The duplicate UI-view shape 0.148+ (and pre-0.147) codex also
    writes for every chat message — must never become a second turn."""
    kind = "user_message" if role == "user" else "agent_message"
    return {"type": "event_msg", "timestamp": ts, "payload": {"type": kind, "message": text}}


def _x_token_count(inp: int, out: int, ts: str) -> dict:
    return {
        "type": "event_msg", "timestamp": ts,
        "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": inp, "output_tokens": out}}},
    }


def _x_compacted(ts: str) -> dict:
    return {"type": "compacted", "timestamp": ts, "payload": {}}


def _x_response_item(ts: str) -> dict:
    return {"type": "response_item", "timestamp": ts, "payload": {"type": "function_call", "name": "exec_command"}}


def _x_low_signal(ts: str) -> dict:
    return {"type": "event_msg", "timestamp": ts, "payload": {"type": "user_message", "message": "ok"}}


CODEX_CORPUS = [
    _x_session_meta(),
    _x_user("Ship the fix for the offset bug", "2026-05-01T09:00:01Z"),
    _x_event_msg_twin("Ship the fix for the offset bug", "user", "2026-05-01T09:00:01Z"),
    _x_response_item("2026-05-01T09:00:02Z"),
    _x_user('<crosstalk from="peer">ignore me</crosstalk>', "2026-05-01T09:00:03Z"),
    _x_agent("Done — committed as abc123.", "2026-05-01T09:00:04Z"),
    _x_event_msg_twin("Done — committed as abc123.", "assistant", "2026-05-01T09:00:04Z"),
    _x_token_count(50, 12, "2026-05-01T09:00:05Z"),
    _x_low_signal("2026-05-01T09:00:06Z"),
    _x_compacted("2026-05-01T09:00:07Z"),
    _x_token_count(80, 30, "2026-05-01T09:00:08Z"),  # absolute snapshot, not additive
]


class TestCodexExtractorGoldenCorpus:
    def test_chat_extracts_without_any_version_context(self):
        """The extractor is version-free: response_item chat is the one
        source in every codex version, so a missing/unknown cli_version
        (e.g. a repaired rollout) must not drop or defer any turn."""
        turn = CodexTurnExtractor().feed(
            _x_user("This must not disappear", "2026-05-01T09:00:01Z"))
        assert turn is not None
        assert turn["content"] == "This must not disappear"

    def test_batch_parse_matches_expected_turn_stream(self):
        turns = _feed_all(CodexTurnExtractor(), CODEX_CORPUS)
        stream = [(t["role"], t["content"]) for t in turns]
        assert stream == [
            ("user", "Ship the fix for the offset bug"),
            ("injected", '<crosstalk from="peer">ignore me</crosstalk>'),
            ("assistant", "Done — committed as abc123."),
        ]

    def test_injected_role_message_id_uses_user_role_for_identity(self):
        """message_id must match what the live viewer overlay computes for
        this text — it always hashes with role='user', regardless of the
        graph's role='injected' — so accepted turn-corrections still resolve."""
        turns = _feed_all(CodexTurnExtractor(), CODEX_CORPUS)
        injected = next(t for t in turns if t["role"] == "injected")
        from tools.dashboard.session_harness import codex_message_id
        expected = codex_message_id({}, "user", injected["content"])
        assert injected["message_id"] == expected

    def test_token_count_is_absolute_not_additive(self):
        ext = CodexTurnExtractor()
        _feed_all(ext, CODEX_CORPUS)
        s = ext.state
        assert s["total_input_tokens"] == 80
        assert s["total_output_tokens"] == 30

    def test_final_state_meta_fields(self):
        ext = CodexTurnExtractor()
        _feed_all(ext, CODEX_CORPUS)
        s = ext.state
        assert s["originator"] == "codex-tui"
        assert s["model_provider"] == "openai"
        assert s["model"] == "gpt-test"


class TestCodexExtractorSplitInvariance:
    def test_every_split_point_matches_full_parse(self):
        full_turns = _feed_all(CodexTurnExtractor(), CODEX_CORPUS)
        for i in range(len(CODEX_CORPUS) + 1):
            a, b = CODEX_CORPUS[:i], CODEX_CORPUS[i:]
            ext_a = CodexTurnExtractor()
            turns_a = _feed_all(ext_a, a)
            ext_b = CodexTurnExtractor.from_state(ext_a.state)
            turns_b = _feed_all(ext_b, b)
            assert turns_a + turns_b == full_turns, f"split at line {i} diverged"

    def test_token_count_resume_keeps_latest_absolute_value(self):
        """Resuming mid-stream must not lose the absolute-snapshot semantics
        — state after split carries the latest value seen, not a partial sum."""
        split_idx = CODEX_CORPUS.index(
            next(e for e in CODEX_CORPUS
                 if e.get("payload", {}).get("type") == "token_count")
        ) + 1
        a, b = CODEX_CORPUS[:split_idx], CODEX_CORPUS[split_idx:]
        ext_a = CodexTurnExtractor()
        _feed_all(ext_a, a)
        assert ext_a.state["total_input_tokens"] == 50

        ext_b = CodexTurnExtractor.from_state(ext_a.state)
        _feed_all(ext_b, b)
        assert ext_b.state["total_input_tokens"] == 80


# ══════════════════════════════════════════════════════════════════════
# File-level parity — parse_*_session still produces the extractor's output
# ══════════════════════════════════════════════════════════════════════


def _write_jsonl(path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class TestBatchParserMatchesExtractor:
    def test_parse_claude_code_session_matches_direct_feed(self, tmp_path):
        from tools.graph.ingest import parse_claude_code_session
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, CLAUDE_CORPUS)

        meta, turns = parse_claude_code_session(jsonl)
        direct_turns = _feed_all(ClaudeTurnExtractor(), CLAUDE_CORPUS)

        assert turns == direct_turns
        assert meta["total_turns"] == len(direct_turns)

    def test_parse_codex_session_matches_direct_feed(self, tmp_path):
        from tools.graph.ingest import parse_codex_session
        jsonl = tmp_path / "rollout-x.jsonl"
        _write_jsonl(jsonl, CODEX_CORPUS)

        meta, turns = parse_codex_session(jsonl)
        direct_turns = _feed_all(CodexTurnExtractor(), CODEX_CORPUS)

        assert turns == direct_turns
        assert meta["total_turns"] == len(direct_turns)
        assert meta["originator"] == "codex-tui"
