"""_has_running_tool decides the stale threshold: 1800s vs 300s.

While a tool runs, the last JSONL line IS the assistant ``tool_use`` — the
``tool_result`` line is not written until the tool finishes. So "last line is an
assistant turn holding a tool_use" is the signal that a tool is in flight, and
it buys the long threshold.

The size of that line is MODEL-CONTROLLED. A large ``Write`` input or a fat MCP
argument produces a single JSONL line far over 8KB. Reading a fixed 8KB tail
started the window mid-JSON, ``json.loads`` raised, the function returned False,
and a legitimately running tool silently dropped to the 300s threshold where it
could be killed mid-flight.

Measured on auto-42rsi's runs 2026-08-16, whose JSONLs carried lines up to 54KB.
Those particular timeouts were NOT caused by this — their final events were
completed ``tool_result``s, for which False is the correct answer, and the model
simply produced no next turn. The hazard here is real and separate.
"""

import json

import pytest

from agents.dispatcher import _has_running_tool


def _write(tmp_path, entry, name="session.jsonl"):
    path = tmp_path / name
    path.write_text('{"type":"user","message":{}}\n' + json.dumps(entry) + "\n")
    return path


def _in_flight_tool_use(payload_bytes):
    """An assistant turn whose tool call is still running."""
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Write",
                    "input": {"file_path": "/x.py", "content": "y" * payload_bytes},
                }
            ]
        },
    }


# 8192 was the old fixed window; straddle it and go well beyond.
@pytest.mark.parametrize("payload", [2_000, 8_000, 8_200, 30_000, 120_000, 400_000])
def test_in_flight_tool_use_gets_the_long_threshold_at_any_line_size(tmp_path, payload):
    path = _write(tmp_path, _in_flight_tool_use(payload), f"a{payload}.jsonl")
    assert _has_running_tool(path) is True


@pytest.mark.parametrize("payload", [100, 20_000])
def test_completed_tool_result_does_not_get_the_long_threshold(tmp_path, payload):
    """A finished tool is not a running one, however large its result.

    This is the case that must NOT regress into True: once the tool_result
    lands, the tool is done and a subsequent stall is a next-turn stall, which
    the short threshold exists to catch.
    """
    entry = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "z" * payload}
            ]
        },
    }
    assert _has_running_tool(_write(tmp_path, entry, f"b{payload}.jsonl")) is False


def test_assistant_turn_without_a_tool_use_is_not_a_running_tool(tmp_path):
    entry = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "x" * 30_000}]},
    }
    assert _has_running_tool(_write(tmp_path, entry)) is False


def test_non_assistant_final_line_is_not_a_running_tool(tmp_path):
    """e.g. the 'last-prompt' record a completed run ends on."""
    entry = {"type": "last-prompt", "prompt": "go"}
    assert _has_running_tool(_write(tmp_path, entry)) is False


@pytest.mark.parametrize(
    "body",
    [
        '{"type":"assistant", "message": {"cont',  # truncated mid-write
        "",  # empty
        "\n\n",  # only blank lines
        "not json at all\n",
    ],
)
def test_unreadable_content_falls_back_to_the_short_threshold(tmp_path, body):
    """Fail closed: never raise, never grant 1800s on a file we cannot parse."""
    path = tmp_path / "broken.jsonl"
    path.write_text(body)
    assert _has_running_tool(path) is False


def test_missing_file_falls_back_to_the_short_threshold(tmp_path):
    assert _has_running_tool(tmp_path / "does-not-exist.jsonl") is False
