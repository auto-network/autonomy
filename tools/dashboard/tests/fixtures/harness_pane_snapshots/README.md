# Harness Pane Snapshot Fixtures

auto-eerfx. Plain-text captures of `tmux capture-pane -p -t <name>`
output for each documented harness state. Used by
`test_harness_screen_state.py` to drive deterministic regression tests
of the screen-state detection logic in
`tools/dashboard/session_harness.py`.

## Files

- `claude_trust_dialog.txt` — dialog visible, no confirm sent yet.
  Used to validate detection AND to assert the confirm keystroke is
  emitted.
- `claude_trust_dialog_confirming.txt` — same dialog snapshot used
  with `current_state.confirming_trust_prompt=True` to verify the
  adapter does NOT emit a second confirm keystroke while the first
  is in flight.
- `claude_trust_dialog_cleared.txt` — captured just after a confirm
  worked; dialog gone, `>` prompt visible. Used to assert the flag
  clears.
- `claude_planning_mode.txt` — Planning… banner visible.
- `claude_composer_ready.txt` — `>` composer prompt at line end, no
  overlays.
- `claude_auth_required.txt` — auth-failure banner ("please run /login").
- `codex_typical.txt` — typical Codex pane (for the stub adapter).

## Capturing new fixtures

Real captures should be scrubbed of any session-identifying text
(workspace names, paths under `/home/`, tmux session names with
timestamps) before commit. Use placeholders like `<workspace>` and
`<session>` where text would otherwise leak.

The detection regexes match on glyph patterns (box-drawing corners,
banner text, prompt shapes) — not on absolute column positions or
pane widths. Fixture line lengths can vary without breaking matches.

## Empirical verification gap

The hypothesis that `C-m` (Enter) on the default-selected option
confirms Claude's trust dialog needs sandbox verification against a
real harness. Capture `claude_trust_dialog.txt` from a real instance,
spawn Claude in a sandbox triggering the dialog, send the confirm
keystroke from `read_screen_state`, and capture
`claude_trust_dialog_cleared.txt` afterward. The pair documents the
empirical confirmation that the keystroke works.

Until that pair is real-captured, the synthetic fixtures here cover
the detection logic but not the confirm-clears-dialog assertion.
