"""CLI tests for ``graph turn-correction suggest`` (auto-edec1.1).

Covers the agent-facing one-shot contract: the command only emits the corrected
replacement text plus optional metadata. Target resolution and raw-text hashing
happen later in SessionMonitor. The emitted JSON is the parser-upconvert
contract — see ``tools/dashboard/tests/test_parser.py`` for the parser side.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

from tools.graph import cli


def _run_cli(argv: list[str], stdin_text: str | None = None) -> tuple[int, str, str]:
    """Drive ``cli.main`` end-to-end. Returns (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    saved_argv = sys.argv
    saved_stdin = sys.stdin
    sys.argv = ["graph"] + argv
    if stdin_text is not None:
        sys.stdin = io.StringIO(stdin_text)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                rc = int(e.code) if e.code is not None else 0
    finally:
        sys.argv = saved_argv
        sys.stdin = saved_stdin
    return rc, out.getvalue(), err.getvalue()


# ── short positional payload ──────────────────────────────────


def test_suggest_short_payload_emits_required_fields():
    rc, out, err = _run_cli([
        "turn-correction", "suggest",
        "JSON encoded message",
        "--json",
    ])
    assert rc == 0, err
    payload = json.loads(out.strip())
    assert payload["type"] == "turn_correction"
    assert payload["version"] == 2
    assert payload["corrected_text"] == "JSON encoded message"
    # Optional fields stay absent when not provided.
    assert "target_message_id" not in payload
    assert "original_sha256" not in payload
    assert "mode" not in payload
    assert "reason" not in payload
    assert "confidence" not in payload


def test_suggest_optional_fields_propagate():
    rc, out, err = _run_cli([
        "turn-correction", "suggest",
        "fixed",
        "--mode", "balanced",
        "--reason", "dictation cleanup",
        "--confidence", "0.9",
        "--json",
    ])
    assert rc == 0, err
    payload = json.loads(out.strip())
    assert payload["mode"] == "balanced"
    assert payload["reason"] == "dictation cleanup"
    assert payload["confidence"] == pytest.approx(0.9)


# ── long stdin-based payload ──────────────────────────────────


def test_suggest_long_stdin_payload_supported():
    """Multi-paragraph corrections must work via --stdin without argv blowup."""
    long_text = "\n\n".join(f"Paragraph {i}: " + ("x" * 400) for i in range(20))
    assert len(long_text) > 8000
    rc, out, err = _run_cli(
        [
            "turn-correction", "suggest",
            "--stdin", "--json",
        ],
        stdin_text=long_text,
    )
    assert rc == 0, err
    payload = json.loads(out.strip())
    assert payload["corrected_text"] == long_text
    assert "target_message_id" not in payload


def test_suggest_long_stdin_payload_supported_via_content_alias():
    long_text = "\n\n".join(f"Paragraph {i}: " + ("x" * 200) for i in range(8))
    rc, out, err = _run_cli(
        [
            "turn-correction", "suggest",
            "-c", "-", "--json",
        ],
        stdin_text=long_text,
    )
    assert rc == 0, err
    payload = json.loads(out.strip())
    assert payload["corrected_text"] == long_text


def test_suggest_stdin_preserves_trailing_newline():
    """``corrected_text`` is the canonical replacement; we don't strip it."""
    rc, out, err = _run_cli(
        [
            "turn-correction", "suggest",
            "--stdin", "--json",
        ],
        stdin_text="line one\nline two\n",
    )
    assert rc == 0, err
    payload = json.loads(out.strip())
    assert payload["corrected_text"] == "line one\nline two\n"


# ── validation ─────────────────────────────────────────────────


def test_suggest_requires_corrected_text():
    rc, _, err = _run_cli([
        "turn-correction", "suggest",
        "--json",
    ])
    assert rc != 0
    assert "corrected text" in err.lower()


def test_suggest_rejects_both_argv_and_stdin():
    rc, _, err = _run_cli(
        [
            "turn-correction", "suggest",
            "from-argv",
            "--stdin", "--json",
        ],
        stdin_text="from-stdin",
    )
    assert rc != 0
    assert "stdin" in err.lower()


def test_suggest_rejects_multiple_input_forms():
    rc, _, err = _run_cli(
        [
            "turn-correction", "suggest",
            "from-argv",
            "-c", "-", "--json",
        ],
        stdin_text="from-stdin",
    )
    assert rc != 0
    assert "multiple ways" in err.lower()


def test_suggest_rejects_invalid_mode():
    rc, _, _ = _run_cli([
        "turn-correction", "suggest", "x",
        "--mode", "wild",
        "--json",
    ])
    assert rc != 0


@pytest.mark.parametrize("bad", ["-0.1", "1.5"])
def test_suggest_rejects_out_of_range_confidence(bad):
    rc, _, err = _run_cli([
        "turn-correction", "suggest", "x",
        "--confidence", bad,
        "--json",
    ])
    assert rc != 0
    assert "confidence" in err.lower()


# ── non-JSON output (human-readable mode) ─────────────────────


def test_suggest_always_emits_json():
    """Output is unconditionally JSON — there is no toggle.

    The previous design had a pretty-print default that silently no-op'd
    from the parser's perspective. Agents would see ✓ and assume success
    while no overlay ever rendered. Removed entirely; there is no
    "make this command not work" mode.
    """
    rc, out, err = _run_cli([
        "turn-correction", "suggest",
        "fixed",
    ])
    assert rc == 0, err
    payload = json.loads(out.strip())
    assert payload["type"] == "turn_correction"
    assert payload["corrected_text"] == "fixed"
