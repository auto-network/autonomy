"""Backend target-resolution for authenticated turn-correction suggestions.

Bead auto-hmow2. Turn correction is delivered as one authenticated dashboard
API call (``POST /api/session/turn-corrections/suggest``), not through the
harness transcript. The CLI sends only the corrected replacement text; the
server derives the caller's session from the bearer token, then uses the
helpers here to pick which recent canonical user turn the correction targets.

These helpers were extracted verbatim (in behavior) from ``session_monitor``'s
former ``_persist_turn_corrections`` machinery so Claude, Codex, queued, and
synthetic message identities keep the one message-id contract: recent user
turns are read back through the same session-harness ``parse_line`` adapters
that assign those ids, never by re-decoding provider-specific wire fields.

Resolution collapse (documented deviation, see auto-hmow2): the live monitor
scored a two-tier deque/history split gated by a 5-minute recency window. That
window was a *live-delivery* artifact — the deque only held turns the monitor
had already streamed. A synchronous POST has no deque; it reads a bounded
snapshot of the JSONL tail (the same ``line_limit`` / ``user_limit`` bounds)
and scores it in one pass at the ACCEPT similarity threshold with the identical
deterministic winner ordering. This never rejects a target the old deque path
would have accepted; it only removes the wall-clock recency gate that no longer
has meaning once delivery is request-synchronous.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


# Bounded snapshot of the recent JSONL tail read on each POST. Same bounds the
# session monitor used for its history fallback so behavior is preserved.
RECENT_USER_LINE_LIMIT = 2_000
RECENT_USER_LIMIT = 100

# Two-tier acceptance, preserved from the former session_monitor resolver:
#  * the most-recent ``RECENT_USER_WINDOW`` turns match at the loose ACCEPT
#    threshold (a live correction almost always targets the latest turn, which
#    may be a heavy rewrite), and
#  * older turns in the bounded snapshot match only at the strict HISTORY
#    threshold, so a short/generic correction cannot latch onto a far-back turn
#    on coincidental partial overlap.
ACCEPT_SIMILARITY = 0.55
HISTORY_SIMILARITY = 0.85
RECENT_USER_WINDOW = 5

_TOKEN_RE = re.compile(r"(\s+|\w+|[^\w\s])", re.UNICODE)

_PUNCTUATION_NORMALIZE_TABLE = str.maketrans({
    "\u2019": "'",
    "\u2018": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\ufe41": '"',
    "\ufe42": '"',
    "\xab": '"',
    "\xbb": '"',
})


def _normalized_token(token: str) -> str:
    return token.translate(_PUNCTUATION_NORMALIZE_TABLE)


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(text)


def _lcs(a: list[str], b: list[str]) -> list[list[int]]:
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if _normalized_token(a[i - 1]) == _normalized_token(b[j - 1]):
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = dp[i - 1][j] if dp[i - 1][j] >= dp[i][j - 1] else dp[i][j - 1]
    return dp


def diff_fragments(raw_text: str, corrected_text: str) -> list[dict[str, str]]:
    """Token-level same/delete/insert fragments between raw and corrected text.

    Merges adjacent same-kind ops. Used both to score a candidate and (in the
    browser, via a parallel JS implementation) to render the inline diff.
    """
    # Outer whitespace is transport noise (for example, a trailing space from
    # dictation). Trim it before token diffing so it never becomes an edit.
    raw_text = raw_text.strip()
    corrected_text = corrected_text.strip()
    if raw_text == corrected_text:
        return [{"kind": "same", "text": raw_text}] if raw_text else []
    if not raw_text:
        return [{"kind": "insert", "text": corrected_text}] if corrected_text else []
    if not corrected_text:
        return [{"kind": "delete", "text": raw_text}]
    a = _tokenize(raw_text)
    b = _tokenize(corrected_text)
    dp = _lcs(a, b)
    ops: list[dict[str, str]] = []
    i, j = len(a), len(b)
    while i > 0 and j > 0:
        if _normalized_token(a[i - 1]) == _normalized_token(b[j - 1]):
            ops.append({"kind": "same", "text": a[i - 1]})
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            ops.append({"kind": "delete", "text": a[i - 1]})
            i -= 1
        else:
            ops.append({"kind": "insert", "text": b[j - 1]})
            j -= 1
    while i > 0:
        ops.append({"kind": "delete", "text": a[i - 1]})
        i -= 1
    while j > 0:
        ops.append({"kind": "insert", "text": b[j - 1]})
        j -= 1
    ops.reverse()
    merged: list[dict[str, str]] = []
    for op in ops:
        if merged and merged[-1]["kind"] == op["kind"]:
            merged[-1]["text"] += op["text"]
        else:
            merged.append(dict(op))
    return merged


def correction_metrics(raw_text: str, corrected_text: str) -> dict[str, Any]:
    """Score how well ``corrected_text`` could be a correction of ``raw_text``.

    Returns a metrics dict whose ``score_key`` is a deterministic sort key
    (lower is better): higher char similarity wins, then fewer edited chars,
    then fewer edit fragments, then a smaller absolute length delta.
    """
    fragments = diff_fragments(raw_text, corrected_text)
    same_chars = sum(len(f["text"]) for f in fragments if f["kind"] == "same")
    delete_chars = sum(len(f["text"]) for f in fragments if f["kind"] == "delete")
    insert_chars = sum(len(f["text"]) for f in fragments if f["kind"] == "insert")
    edit_fragments = sum(1 for f in fragments if f["kind"] != "same")
    char_similarity = SequenceMatcher(
        None,
        raw_text.lower(),
        corrected_text.lower(),
        autojunk=False,
    ).ratio()
    total_len = max(len(raw_text) + len(corrected_text), 1)
    edit_chars = delete_chars + insert_chars
    edit_ratio = edit_chars / total_len
    acceptable = char_similarity >= ACCEPT_SIMILARITY
    return {
        "fragments": fragments,
        "same_chars": same_chars,
        "delete_chars": delete_chars,
        "insert_chars": insert_chars,
        "edit_chars": edit_chars,
        "edit_fragments": edit_fragments,
        "char_similarity": char_similarity,
        "edit_ratio": edit_ratio,
        "acceptable": acceptable,
        "score_key": (
            -int(round(char_similarity * 1000)),
            edit_chars,
            edit_fragments,
            abs(len(raw_text) - len(corrected_text)),
        ),
    }


def read_recent_canonical_user_turns(
    jsonl_path: str | None,
    *,
    line_limit: int = RECENT_USER_LINE_LIMIT,
    user_limit: int | None = RECENT_USER_LIMIT,
) -> list[dict[str, Any]]:
    """Return recent canonical ``user`` turns from a session JSONL tail.

    Reads the last ``line_limit`` lines and parses them through the session
    harness adapter for the path so Claude, Codex, queued, and synthetic
    message identities all resolve to the same ``message_id`` every other
    consumer (viewer overlay, graph ingest) uses. Oldest-first; the last
    ``user_limit`` entries when bounded. File I/O and individual malformed
    records are best-effort; missing mandatory transcript metadata is raised
    so the caller cannot mistake an unparseable transcript for no messages.
    """
    if not jsonl_path:
        return []
    try:
        path = Path(jsonl_path)
        all_lines = path.read_text(errors="replace").splitlines()
        lines = all_lines[-line_limit:]
    except OSError:
        return []

    # Local import: session_harness imports lightweight stdlib only, but keep
    # the dependency direction explicit and avoid an import cycle at module
    # load (session_monitor imports this module).
    from tools.dashboard.session_harness import (
        TranscriptParseContextError,
        resolve_harness_for_path,
    )

    reader = resolve_harness_for_path(path)

    users: list[dict[str, Any]] = []
    for raw in lines:
        try:
            parsed = reader.parse_line(raw)
        except TranscriptParseContextError:
            raise
        except Exception:
            parsed = None
        parsed_entries = parsed if isinstance(parsed, list) else [parsed] if parsed else []
        for entry in parsed_entries:
            if not isinstance(entry, dict) or entry.get("type") != "user":
                continue
            message_id = entry.get("message_id")
            content = entry.get("content")
            if not isinstance(message_id, str) or not message_id:
                continue
            if not isinstance(content, str) or not content:
                continue
            users.append({
                "message_id": message_id,
                "content": content,
                "timestamp": entry.get("timestamp", "") or "",
            })

    if user_limit is not None and len(users) > user_limit:
        return users[-user_limit:]
    return users


def resolve_best_correction_target(
    *,
    users: Iterable[dict[str, Any]],
    corrected_text: str,
    unavailable: Optional[Callable[[str], bool]] = None,
    recent_window: int = RECENT_USER_WINDOW,
) -> Optional[dict[str, Any]]:
    """Pick the recent user turn a correction most likely targets.

    ``users`` is an oldest-first snapshot (as returned by
    :func:`read_recent_canonical_user_turns`). Each candidate is scored against
    ``corrected_text``; the winner is the acceptable candidate with the best
    ``score_key``, ties broken toward the most recent turn.

    Two-tier acceptance is preserved from the former monitor resolver: the most
    recent ``recent_window`` turns need only clear :data:`ACCEPT_SIMILARITY`
    (0.55), while older turns in the snapshot must clear the stricter
    :data:`HISTORY_SIMILARITY` (0.85) so a short/generic correction cannot latch
    onto a far-back turn on coincidental overlap.

    ``unavailable(message_id)`` lets the caller exclude targets that already
    carry a *terminal* correction (an accepted/dismissed row must not be
    re-targeted by a fresh pending suggestion). Pending rows are left available
    so the DAO's pending-upsert path can refresh them.

    Returns the winning user dict augmented with ``metrics``, or ``None`` when
    no candidate clears its tier's threshold.
    """
    user_list = [u for u in users if isinstance(u, dict)]
    winner: Optional[dict[str, Any]] = None
    seen_ids: set[str] = set()

    # Iterate newest-first so ``order`` (0 = most recent) both selects the
    # similarity tier and breaks score ties toward the latest turn.
    for order, user in enumerate(reversed(user_list)):
        message_id = str(user.get("message_id") or "")
        if not message_id or message_id in seen_ids:
            continue
        seen_ids.add(message_id)
        if unavailable is not None and unavailable(message_id):
            continue
        content = str(user.get("content") or "")
        metrics = correction_metrics(content, corrected_text)
        threshold = ACCEPT_SIMILARITY if order < recent_window else HISTORY_SIMILARITY
        if not metrics["acceptable"] or float(metrics["char_similarity"]) < threshold:
            continue
        candidate = dict(user)
        candidate["order"] = order
        candidate["metrics"] = metrics
        if winner is None:
            winner = candidate
            continue
        winner_key = winner["metrics"]["score_key"] + (winner["order"],)
        candidate_key = metrics["score_key"] + (order,)
        if candidate_key < winner_key:
            winner = candidate

    return winner
