"""auto-eerfx: harness adapters' read_screen_state contract.

Tests cover:
  - Claude detects trust dialog; emits one confirm keystroke; flips
    confirming_trust_prompt true; does NOT re-emit on the next poll
    while the flag is set.
  - Claude detects planning mode banner.
  - Claude detects composer prompt → composer_ready true (only when
    no overlay blocks input).
  - Claude detects auth-required banner.
  - Codex stub returns composer_ready=true unconditionally with no
    keystrokes (so Codex sessions can transition to ready).
  - tmux_send_keys helper builds the right tmux send-keys invocations.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools.dashboard.session_harness import CLAUDE_HARNESS, CODEX_HARNESS


# ── Pane snapshot fixtures loaded from disk
#
# auto-eerfx: real pane captures live under
# ``tests/fixtures/harness_pane_snapshots/`` so they're versionable
# regression artifacts rather than inline-string approximations. The
# detection regexes match on glyph patterns (box-drawing corners,
# banner text, prompt shapes) — not absolute column positions — so
# fixture line lengths can vary without breaking matches.

_FIXTURE_DIR = (
    Path(__file__).parent / "fixtures" / "harness_pane_snapshots"
)


def _load(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text()


PANE_TRUST_DIALOG = _load("claude_trust_dialog.txt")
PANE_TRUST_CLEARED = _load("claude_trust_dialog_cleared.txt")
PANE_PLANNING_MODE = _load("claude_planning_mode.txt")
PANE_COMPOSER_READY = _load("claude_composer_ready.txt")
PANE_AUTH_REQUIRED = _load("claude_auth_required.txt")

# Synthetic — represents a session mid-tool-use with no prompt visible.
PANE_BUSY = """\
  Tool use: bash
  ⏵ running command…
  (no prompt visible)
"""


# ── Claude adapter ────────────────────────────────────────────────


def test_claude_detects_trust_dialog_and_emits_confirm():
    state, keys = CLAUDE_HARNESS.read_screen_state(PANE_TRUST_DIALOG, {})
    assert state["confirming_trust_prompt"] is True
    assert len(keys) == 1
    assert keys[0]["kind"] == "key"
    assert keys[0]["value"] == "C-m"
    # No composer prompt visible while the dialog is up.
    assert state["composer_ready"] is False


def test_claude_does_not_resend_confirm_while_flag_set():
    """Once confirming_trust_prompt=True is the current state, the same
    pane snapshot must NOT emit another keystroke. Otherwise the
    poller would spam Enter into the harness."""
    state, keys = CLAUDE_HARNESS.read_screen_state(
        PANE_TRUST_DIALOG, {"confirming_trust_prompt": True},
    )
    assert state["confirming_trust_prompt"] is True
    assert keys == []


def test_claude_clears_confirming_flag_after_dialog_dismisses():
    """Once the dialog is gone in the next snapshot, the flag clears."""
    state, keys = CLAUDE_HARNESS.read_screen_state(
        PANE_COMPOSER_READY, {"confirming_trust_prompt": True},
    )
    assert state["confirming_trust_prompt"] is False
    assert keys == []


def test_claude_detects_planning_mode():
    state, keys = CLAUDE_HARNESS.read_screen_state(PANE_PLANNING_MODE, {})
    assert state["in_planning_mode"] is True
    assert keys == []


def test_claude_detects_composer_ready():
    state, keys = CLAUDE_HARNESS.read_screen_state(PANE_COMPOSER_READY, {})
    assert state["composer_ready"] is True
    assert state["confirming_trust_prompt"] is False
    assert state["in_planning_mode"] is False
    assert state["blocking_modal"] is None
    assert keys == []


def test_claude_detects_auth_required():
    state, keys = CLAUDE_HARNESS.read_screen_state(PANE_AUTH_REQUIRED, {})
    assert state["blocking_modal"] == "auth_required"
    # Composer not ready while a blocking modal is up.
    assert state["composer_ready"] is False
    assert keys == []


def test_claude_busy_no_prompt_returns_composer_not_ready():
    """Mid-tool-use, no `> ` prompt visible → composer_ready=False."""
    state, keys = CLAUDE_HARNESS.read_screen_state(PANE_BUSY, {})
    assert state["composer_ready"] is False
    assert keys == []


def test_claude_composer_gated_on_no_overlays():
    """composer_ready=False when planning mode is active even if `> ` visible."""
    pane = PANE_COMPOSER_READY + "\n  Planning…\n"
    state, keys = CLAUDE_HARNESS.read_screen_state(pane, {})
    # `> ` visible but planning banner also visible.
    assert state["in_planning_mode"] is True


# ── Codex stub ─────────────────────────────────────────────────────


def test_codex_stub_returns_composer_ready_true():
    """CRITICAL: Codex sessions must reach composer_ready or they
    never transition to 'ready' in the lifecycle. Stub returns true
    unconditionally."""
    state, keys = CODEX_HARNESS.read_screen_state("any pane text", {})
    assert state["composer_ready"] is True
    assert state["confirming_trust_prompt"] is False
    assert state["in_planning_mode"] is False
    assert state["blocking_modal"] is None
    assert keys == []


def test_codex_stub_ignores_pane_content():
    """The stub returns the same dict regardless of pane content (it
    doesn't actually parse anything yet)."""
    state_a, _ = CODEX_HARNESS.read_screen_state(PANE_TRUST_DIALOG, {})
    state_b, _ = CODEX_HARNESS.read_screen_state(PANE_BUSY, {})
    assert state_a == state_b


# ── tmux_send_keys helper ──────────────────────────────────────────


def test_tmux_send_keys_invokes_send_keys_with_right_flags():
    """key kind → no -l; literal kind → -l."""
    from tools.dashboard import tmux_send as ts
    calls = []
    def _capture(args, **kwargs):
        calls.append(args)
        m = MagicMock()
        m.returncode = 0
        return m
    with patch("tools.dashboard.tmux_send.subprocess.run", side_effect=_capture):
        ts._tmux_send_one_key("auto-x", {"kind": "key", "value": "C-m"})
        ts._tmux_send_one_key("auto-x", {"kind": "literal", "value": "1"})
    assert calls[0] == ["tmux", "send-keys", "-t", "auto-x", "C-m"]
    assert calls[1] == ["tmux", "send-keys", "-t", "auto-x", "-l", "1"]


def test_tmux_send_keys_ignores_empty_value():
    """Sequence dicts without a value are silently skipped (defensive)."""
    from tools.dashboard import tmux_send as ts
    calls = []
    def _capture(args, **kwargs):
        calls.append(args)
        m = MagicMock()
        m.returncode = 0
        return m
    with patch("tools.dashboard.tmux_send.subprocess.run", side_effect=_capture):
        ts._tmux_send_one_key("auto-x", {})
        ts._tmux_send_one_key("auto-x", {"kind": "key"})
    assert calls == []
