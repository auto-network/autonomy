"""L1 tests for the extracted turn-correction resolver (auto-hmow2).

Covers ``tools/dashboard/turn_corrections``:

  * ``correction_metrics`` — diff/similarity scoring (ported from the former
    ``session_monitor._turn_correction_metrics`` tests).
  * ``read_recent_canonical_user_turns`` — reads recent user turns through the
    canonical harness adapters (Claude + Codex) so message identities match.
  * ``resolve_best_correction_target`` — deterministic winner selection,
    unrelated-text rejection, terminal-target exclusion, no-target behavior.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from tools.dashboard import turn_corrections as tc


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── correction_metrics ─────────────────────────────────────────


def test_metrics_accepts_long_dictation_cleanup():
    raw = (
        "It worked and updated live. Congratulations I think the future is finally almost landed.\n\n"
        "Now the next thing to do is product conversation, which means primers which explain the command "
        "and explain when it should be used, which we want to biased towards using it aggressively I would "
        "not be angry if almost every single one of my messages got a correction if it meant cleaning up the log."
    )
    corrected = (
        "It worked and updated live. Congratulations. I think the future is finally almost landed.\n\n"
        "Now the next thing to do is product conversation, which means primers that explain the command and "
        "explain when it should be used. We want to be biased toward using it aggressively. I would not be "
        "angry if almost every single one of my messages got a correction if it meant cleaning up the log."
    )
    metrics = tc.correction_metrics(raw, corrected)
    assert metrics["acceptable"] is True
    assert metrics["char_similarity"] >= 0.80


def test_metrics_rejects_unrelated_message():
    raw = "Proceed"
    corrected = (
        "It worked and updated live. Congratulations. I think the future is finally almost landed.\n\n"
        "Now the next thing to do is product conversation, which means primers that explain the command."
    )
    metrics = tc.correction_metrics(raw, corrected)
    assert metrics["acceptable"] is False
    assert metrics["char_similarity"] < 0.25


def test_metrics_rejects_weak_recent_overlap():
    raw = "Try it see if it works to repro the issue."
    corrected = "Ok so what’s current state and next steps?"
    metrics = tc.correction_metrics(raw, corrected)
    assert metrics["acceptable"] is False
    assert metrics["char_similarity"] < 0.55


def test_metrics_prefers_related_long_message_over_unrelated_short_one():
    good_raw = "I’m gonna write a message which you can away a correction to."
    bad_raw = "So now try to apply the correction and we’ll see if it can match it"
    corrected = "I’m gonna write a message which you can apply a correction to."
    good = tc.correction_metrics(good_raw, corrected)
    bad = tc.correction_metrics(bad_raw, corrected)
    assert good["score_key"] < bad["score_key"]


def _extract(frags: list[dict[str, str]], kind: str) -> str:
    return "".join(f["text"] for f in frags if f["kind"] == kind)


def test_diff_fragments_single_word_typo():
    frags = tc.diff_fragments("Plese review", "Please review")
    kinds = {f["kind"] for f in frags}
    assert "delete" in kinds and "insert" in kinds
    assert _extract(frags, "delete") == "Plese"
    assert _extract(frags, "insert") == "Please"


def test_diff_fragments_curly_apostrophe_vs_straight_is_no_op_when_normalized():
    """Normalize curly/straight apostrophe variants as non-substantive style edits."""
    frags = tc.diff_fragments("I won’t go", "I won't go")
    assert {f["kind"] for f in frags} == {"same"}
    assert _extract(frags, "delete") == ""
    assert _extract(frags, "insert") == ""
    assert "".join(f["text"] for f in frags) == "I won’t go"


def test_diff_fragments_comma_insertion_highlights_punctuation_only():
    """Comma insertion should be highlighted as comma-only punctuation."""
    frags = tc.diff_fragments("Please review this", "Please review this,")
    assert _extract(frags, "insert") == ","
    assert _extract(frags, "delete") == ""
    assert "".join(f["text"] for f in frags) == "Please review this,"


def test_diff_fragments_curly_quotes_are_equivalent_to_straight():
    """Normalize smart quotes to plain quote variants in correction matching."""
    frags = tc.diff_fragments('“Hello”', '"Hello"')
    assert {f["kind"] for f in frags} == {"same"}
    assert _extract(frags, "delete") == ""
    assert _extract(frags, "insert") == ""


def test_correction_metrics_small_punctuation_style_change():
    """Punctuation-style-only edits should not look like user corrections."""
    metrics = tc.correction_metrics("I won’t go", "I won't go")
    assert metrics["acceptable"] is True
    assert metrics["edit_chars"] == 0
    assert metrics["edit_fragments"] == 0
    assert len(metrics["fragments"]) == 1


def test_host_exported_correction_with_only_punctuation_delta_flags_only_punctuation_ops():
    """Use a real correction sample that differs by a slash/spacing style edit."""
    # Host-exported pending row:
    # raw: "... capture /current tmux state?"
    # corrected: "... capture / current tmux state?"
    frags = tc.diff_fragments(
        "Is there an API you can use to see the tmux screen capture /current tmux state?",
        "Is there an API you can use to see the tmux screen capture / current tmux state?",
    )
    assert frags, "expected diff fragments to be computed"
    punctuation_only_ops = all(
        not re.search(r"[A-Za-z0-9]", part["text"])
        for part in frags
        if part["kind"] != "same"
    )
    assert punctuation_only_ops is True



# ── read_recent_canonical_user_turns (Claude + Codex) ──────────


def _write_jsonl(path: Path, lines: list[dict]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")
    return str(path)


def test_reads_claude_user_turns_with_uuid_message_id(tmp_path):
    jsonl = _write_jsonl(tmp_path / "sessions" / "u" / "claude.jsonl", [
        {"type": "user", "uuid": "u-1",
         "message": {"role": "user", "content": [{"type": "text", "text": "first message"}]},
         "timestamp": "2026-08-10T12:00:00Z"},
        {"type": "assistant", "uuid": "a-1",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "reply"}]},
         "timestamp": "2026-08-10T12:00:01Z"},
        {"type": "user", "uuid": "u-2",
         "message": {"role": "user", "content": [{"type": "text", "text": "second message"}]},
         "timestamp": "2026-08-10T12:00:02Z"},
    ])
    users = tc.read_recent_canonical_user_turns(jsonl)
    assert [u["message_id"] for u in users] == ["u-1", "u-2"]
    assert [u["content"] for u in users] == ["first message", "second message"]


def test_reads_codex_user_turns_with_synthetic_message_id(tmp_path):
    # Codex user turns without a raw uuid get a synthetic
    # ``codex-user:<hash>`` id — the resolver must read that same id.
    # Chat parses from response_item.message only; the event_msg twin that
    # 0.146/0.148+ rollouts also carry never produces a (duplicate) turn.
    from tools.dashboard.session_harness import codex_message_id
    text = "codex user message here"
    expected_id = codex_message_id({}, "user", text)
    assert expected_id and expected_id.startswith("codex-user:")
    # Filename ``rollout-*`` makes resolve_harness_for_path pick the Codex adapter.
    jsonl = _write_jsonl(tmp_path / "sessions" / "u" / "rollout-2026.jsonl", [
        {"type": "session_meta", "payload": {"cli_version": "0.146.0"},
         "timestamp": "2026-08-10T11:59:59Z"},
        {"type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": text}]},
         "timestamp": "2026-08-10T12:00:00Z"},
        {"type": "event_msg", "payload": {"type": "user_message", "message": text},
         "timestamp": "2026-08-10T12:00:00Z"},
    ])
    users = tc.read_recent_canonical_user_turns(jsonl)
    assert len(users) == 1
    assert users[0]["message_id"] == expected_id
    assert users[0]["content"] == text


def test_reads_current_codex_response_item_user_turns_from_bounded_tail(tmp_path):
    """The resolver preserves session_meta context even when it precedes the tail."""
    from tools.dashboard.session_harness import codex_message_id

    text = "Plese fix the correction overlay"
    expected_id = codex_message_id({}, "user", text)
    lines = [{
        "type": "session_meta",
        "payload": {"cli_version": "0.148.0", "originator": "codex-tui"},
        "timestamp": "2026-08-16T12:00:00Z",
    }]
    lines.extend(
        {"type": "event_msg", "payload": {"type": "task_started"}}
        for _ in range(5)
    )
    lines.append({
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
        "timestamp": "2026-08-16T12:01:00Z",
    })
    jsonl = _write_jsonl(
        tmp_path / "sessions" / "u" / "rollout-2026.jsonl", lines)

    users = tc.read_recent_canonical_user_turns(jsonl, line_limit=2)

    assert users == [{
        "message_id": expected_id,
        "content": text,
        "timestamp": "2026-08-16T12:01:00Z",
    }]


def test_missing_or_unreadable_jsonl_yields_empty(tmp_path):
    assert tc.read_recent_canonical_user_turns(None) == []
    assert tc.read_recent_canonical_user_turns(str(tmp_path / "nope.jsonl")) == []


def test_user_limit_bounds_snapshot(tmp_path):
    lines = []
    for i in range(10):
        lines.append({"type": "user", "uuid": f"u-{i}",
                      "message": {"role": "user", "content": [{"type": "text", "text": f"m{i}"}]},
                      "timestamp": "2026-08-10T12:00:00Z"})
    jsonl = _write_jsonl(tmp_path / "sessions" / "u" / "c.jsonl", lines)
    users = tc.read_recent_canonical_user_turns(jsonl, user_limit=3)
    assert [u["message_id"] for u in users] == ["u-7", "u-8", "u-9"]


# ── resolve_best_correction_target ─────────────────────────────


def _user(mid, content, ts="2026-08-10T12:00:00Z"):
    return {"message_id": mid, "content": content, "timestamp": ts}


def test_resolve_picks_best_similarity_match():
    users = [
        _user("m1", "totally different topic about deployment"),
        _user("m2", "Plese reviw the corections API"),
    ]
    target = tc.resolve_best_correction_target(
        users=users, corrected_text="Please review the corrections API")
    assert target is not None
    assert target["message_id"] == "m2"


def test_resolve_normalizes_punctuation_style_variants_in_similarity():
    """Punctuation-style-only differences should not block matching the intended turn."""
    users = [
        _user("m1", "I won’t send this in a second."),
        _user("m2", "I can't send this in a second."),
    ]
    target = tc.resolve_best_correction_target(
        users=users, corrected_text="I won't send this in a second.")
    assert target is not None
    assert target["message_id"] == "m1"


def test_resolve_returns_none_for_unrelated_text():
    users = [_user("m1", "Proceed"), _user("m2", "Try it and see")]
    target = tc.resolve_best_correction_target(
        users=users,
        corrected_text=(
            "An entirely unrelated multi-sentence replacement that shares almost "
            "no vocabulary with either short candidate message above."
        ),
    )
    assert target is None


def test_resolve_returns_none_for_empty_users():
    assert tc.resolve_best_correction_target(users=[], corrected_text="anything") is None


def test_resolve_ties_break_toward_most_recent():
    # Two identical candidates; the most-recent (last in list) wins.
    users = [
        _user("older", "apply a correction here please"),
        _user("newer", "apply a correction here please"),
    ]
    target = tc.resolve_best_correction_target(
        users=users, corrected_text="apply a correction here please now")
    assert target["message_id"] == "newer"


def test_resolve_skips_unavailable_terminal_target():
    users = [
        _user("terminal", "Please review the corrections API"),
        _user("fresh", "Please review the corectons API"),
    ]
    # 'terminal' is the better literal match but is excluded as unavailable;
    # resolution falls through to the next acceptable candidate.
    target = tc.resolve_best_correction_target(
        users=users,
        corrected_text="Please review the corrections API",
        unavailable=lambda mid: mid == "terminal",
    )
    assert target is not None
    assert target["message_id"] == "fresh"


def _unrelated(n):
    return [_user(f"noise-{i}", f"let us deploy service number {i} to production now")
            for i in range(n)]


def test_resolve_two_tier_rejects_far_back_moderate_match():
    """A moderate (~0.6-0.8) match far back in the snapshot needs the strict
    0.85 tier and is rejected; the same text as the most-recent turn passes the
    loose 0.55 tier."""
    corrected = "Please review the corrections API before merging the change"
    moderate = "please review the corrections"
    # order >= recent_window (moderate is oldest, 5 newer turns follow it).
    far_back = tc.resolve_best_correction_target(
        users=[_user("moderate", moderate)] + _unrelated(5),
        corrected_text=corrected, recent_window=5)
    assert far_back is None, "far-back moderate match must fail the strict tier"

    # Same moderate match as the MOST-recent turn clears the loose tier.
    recent = tc.resolve_best_correction_target(
        users=_unrelated(5) + [_user("moderate", moderate)],
        corrected_text=corrected, recent_window=5)
    assert recent is not None and recent["message_id"] == "moderate"


def test_resolve_two_tier_accepts_far_back_strong_match():
    corrected = "Please review the corrections API before merging the change"
    strong = "Please review the corrections API before merging the chnge"  # 1 typo
    target = tc.resolve_best_correction_target(
        users=[_user("strong", strong)] + _unrelated(6),
        corrected_text=corrected, recent_window=5)
    assert target is not None and target["message_id"] == "strong"


def test_resolve_matches_long_correction():
    raw = (
        "we need to productize the primers so agents no when to use the corection command "
        "and it should be biassed towards agressive use"
    )
    corrected = (
        "We need to productize the primers so agents know when to use the correction command, "
        "and it should be biased toward aggressive use."
    )
    users = [_user("noise", "unrelated short note"), _user("target", raw)]
    target = tc.resolve_best_correction_target(users=users, corrected_text=corrected)
    assert target is not None
    assert target["message_id"] == "target"
