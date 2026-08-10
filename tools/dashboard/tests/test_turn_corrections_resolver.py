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


def test_diff_fragments_single_word_typo():
    frags = tc.diff_fragments("Plese review", "Please review")
    kinds = {f["kind"] for f in frags}
    assert "delete" in kinds and "insert" in kinds
    assert "".join(f["text"] for f in frags if f["kind"] == "delete") == "Plese"
    assert "".join(f["text"] for f in frags if f["kind"] == "insert") == "Please"


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
    # Codex event_msg user turns without a raw uuid get a synthetic
    # ``codex-user:<hash>`` id — the resolver must read that same id.
    from tools.dashboard.session_harness import codex_message_id
    text = "codex user message here"
    expected_id = codex_message_id({}, "user", text)
    assert expected_id and expected_id.startswith("codex-user:")
    # Filename ``rollout-*`` makes resolve_harness_for_path pick the Codex adapter.
    jsonl = _write_jsonl(tmp_path / "sessions" / "u" / "rollout-2026.jsonl", [
        {"type": "event_msg", "payload": {"type": "user_message", "message": text},
         "timestamp": "2026-08-10T12:00:00Z"},
    ])
    users = tc.read_recent_canonical_user_turns(jsonl)
    assert len(users) == 1
    assert users[0]["message_id"] == expected_id
    assert users[0]["content"] == text


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
