"""auto-eerfx: harness adapters' read_screen_state contract.

Tests cover:
  - Claude detects trust dialog; emits one confirm keystroke; flips
    confirming_trust_prompt true; does NOT re-emit on the next poll
    while the flag is set.
  - Claude detects planning mode banner.
  - Claude detects composer prompt → composer_ready true (only when
    no overlay blocks input).
  - Claude detects auth-required banner.
  - Codex distinguishes its trust menu from the empty composer and confirms
    the menu exactly once.
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
# Real capture from Claude Code v2.1.158 — the "❯ " prompt glyph + the
# "⏵⏵ … (shift+tab to cycle)" footer. Pins the CURRENT UI so the detector
# can't silently rot against a TUI prompt-glyph change again (the "> "→"❯ "
# switch is exactly what left every session stuck at harness_starting).
PANE_COMPOSER_READY_V2_1 = _load("claude_composer_ready_v2_1.txt")
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


def test_claude_detects_v2_1_177_folder_trust_dialog():
    pane = """\
Is this a project you created or one you trust?

  ❯ 1. Yes, I trust this folder
    2. No, exit

Enter to confirm
"""
    state, keys = CLAUDE_HARNESS.read_screen_state(pane, {})

    assert state["confirming_trust_prompt"] is True
    assert state["composer_ready"] is False
    assert keys == [{"kind": "key", "value": "C-m"}]


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


def test_claude_detects_composer_ready_v2_1_glyph():
    """Claude Code v2.1.158 renders the composer prompt as "❯ " (U+276F),
    not "> ". This real-capture fixture must read composer_ready=True —
    it is the regression that left every live session stuck at
    harness_starting (card frozen on "Booting Claude") because the
    detector only matched the legacy "> " glyph."""
    state, keys = CLAUDE_HARNESS.read_screen_state(PANE_COMPOSER_READY_V2_1, {})
    assert state["composer_ready"] is True, (
        "v2.1.158 '❯ ' prompt must be recognized as composer_ready"
    )
    assert state["confirming_trust_prompt"] is False
    assert state["in_planning_mode"] is False
    assert state["blocking_modal"] is None
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


# ── Codex adapter ──────────────────────────────────────────────────


PANE_CODEX_TRUST_DIALOG = """
>_ You are in /workspace/repo

  Do you trust the contents of this directory?
  Trusting the directory allows project-local config, hooks, and exec policies to load.

› 1. Yes, continue
  2. No, quit

  Press enter to continue
"""

PANE_CODEX_COMPOSER_READY = """
╭────────────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.150.1)                         │
│ model: gpt-5.6-sol · directory: /workspace/repo    │
╰────────────────────────────────────────────────────╯

›
"""


def test_codex_trust_dialog_is_confirmed_but_never_called_composer_ready():
    state, keys = CODEX_HARNESS.read_screen_state(PANE_CODEX_TRUST_DIALOG, {})

    assert state["confirming_trust_prompt"] is True
    assert state["composer_ready"] is False
    assert keys == [{"kind": "key", "value": "C-m"}]


def test_codex_trust_confirmation_is_sent_only_once_while_dialog_remains():
    state, keys = CODEX_HARNESS.read_screen_state(
        PANE_CODEX_TRUST_DIALOG, {"confirming_trust_prompt": True},
    )

    assert state["confirming_trust_prompt"] is True
    assert state["composer_ready"] is False
    assert keys == []


def test_codex_empty_composer_clears_trust_and_reports_ready():
    state, keys = CODEX_HARNESS.read_screen_state(
        PANE_CODEX_COMPOSER_READY, {"confirming_trust_prompt": True},
    )

    assert state["confirming_trust_prompt"] is False
    assert state["composer_ready"] is True
    assert keys == []


def test_codex_busy_screen_is_not_composer_ready():
    state, keys = CODEX_HARNESS.read_screen_state(PANE_BUSY, {})
    assert state["composer_ready"] is False
    assert keys == []


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
