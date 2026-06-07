"""self_repair.py — detector self-repair reporter.

When a detector that *expected* an event times out without it, but we DID
capture the input it could not parse (a tmux screen snapshot, a JSONL line
that failed to parse, ...), file a structured bug bead with that evidence
so an agent (the librarian) can pick it up and fix the detector — e.g.
update a stale regex.

This is loop #1 of the self-refining system: **detect → file → fix**.
The first consumer is the screen-poll loop's trust/composer detection, but
``report_detector_miss()`` is intentionally generic: any "expected event +
timeout + captured-but-unparsed input" site can call it.

Later phases (validate the fix → write a graph note → distribute the lesson)
build on the same bead: the librarian's fix re-runs the detector against the
embedded evidence to validate, then a note propagates the wording/format
change so every user benefits. None of that lives here yet — this module's
job is to turn a silent detector miss into an actionable, evidence-bearing
ticket.

Design constraints (all load-bearing — a self-repair path that misbehaves is
worse than none):

* **Never break the caller.** Every public entry point swallows its own
  exceptions. A failure to file a ticket must not take down the poll loop.
* **Deduped.** The poll loop may observe the same miss every 2s for minutes.
  We file at most once per (detector, evidence-signature) within a cooldown.
* **Non-blocking.** Bead filing shells out to the ``graph`` CLI inside a
  thread so the event loop is never stalled on a subprocess.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# tools/dashboard/self_repair.py -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# (detector, evidence_signature) -> wall ts of last filing. Process-local;
# resets on dashboard restart (re-filing once after a restart is harmless —
# the cooldown only exists to stop steady-state spam from a 2s poll).
_FILED: dict[tuple[str, str], float] = {}

# Don't re-file the same miss for this long. A genuinely different screen
# (different signature) files immediately; the same stuck screen waits.
_DEFAULT_COOLDOWN_S = 6 * 3600

# Cap embedded evidence so a runaway pane can't bloat the bead body.
_EVIDENCE_MAX_CHARS = 6000


def _signature(detector: str, evidence: str) -> str:
    """Coarse content signature for dedup.

    Collapse whitespace so cosmetic churn (cursor position, blank-line
    redraws, trailing spaces) maps to the same signature, while a genuinely
    different screen produces a different one. Bounded so a huge pane doesn't
    dominate hashing cost.
    """
    norm = " ".join((evidence or "").split())[:2000]
    digest = hashlib.sha1(f"{detector}\n{norm}".encode("utf-8", "replace"))
    return digest.hexdigest()[:16]


def _already_filed(detector: str, sig: str, cooldown_s: float, now: float) -> bool:
    last = _FILED.get((detector, sig))
    return last is not None and (now - last) < cooldown_s


def _build_description(
    detector: str,
    expected: str,
    evidence: str,
    evidence_kind: str,
    code_ref: str | None,
    context: dict | None,
    sig: str,
) -> str:
    """Render the bead body: what was expected, what we have, and the
    standing instruction for the librarian — including the validate → note
    follow-through so the fix closes its own loop.

    A ``tags:`` marker line is embedded because ``graph bead`` has no
    ``--tags`` flag; FTS over the body is how the librarian discovers these.
    """
    lines: list[str] = [
        f"# Self-repair: detector `{detector}` timed out",
        "",
        "tags: self-repair, detector-miss, " + detector,
        "",
        "A detector waited for an event that never arrived within its timeout,",
        "but we captured the input it could not parse. The likely cause is that",
        "the thing it parses drifted — a TUI wording/glyph change, a new output",
        "format — and the detector's pattern is now stale.",
        "",
        f"- **Detector:** {detector}",
        f"- **Expected event:** {expected}",
        f"- **Evidence kind:** {evidence_kind}",
        f"- **Evidence signature:** {sig}",
    ]
    if code_ref:
        lines.append(f"- **Detector code:** {code_ref}")
    for key, value in (context or {}).items():
        lines.append(f"- **{key}:** {value}")
    lines += [
        "",
        "## What to do (closes the loop)",
        "1. Read the captured evidence below and compare it against the",
        "   detector's current pattern.",
        "2. Update the pattern to match the current input.",
        "3. **Validate:** re-run the detector against this evidence and confirm",
        "   it now fires (and still passes its existing cases — no regressions).",
        "4. **Write a graph note** capturing the wording/format change so the",
        "   lesson propagates to every user, not just this fix.",
        "",
        "## Captured evidence",
        "```",
        (evidence or "")[:_EVIDENCE_MAX_CHARS],
        "```",
    ]
    return "\n".join(lines)


def _parse_bead_id(stdout: str) -> str | None:
    match = re.search(r"\bauto-[a-z0-9]+\b", stdout or "")
    return match.group(0) if match else None


def _file_bead(
    detector: str,
    expected: str,
    evidence: str,
    evidence_kind: str,
    code_ref: str | None,
    context: dict | None,
    sig: str,
) -> str | None:
    """Shell out to ``graph bead`` to file the ticket. Runs in a worker
    thread (see ``report_detector_miss``). Returns the new bead id, or None
    on any failure (logged, never raised)."""
    desc = _build_description(
        detector, expected, evidence, evidence_kind, code_ref, context, sig
    )
    title = f"[self-repair] {detector}: timed out waiting for {expected}"
    cmd = [
        sys.executable, "-m", "tools.graph", "bead", title,
        "-t", "bug",
        "-p", "1",
        "-d", "-",  # read description from stdin (avoids arg-length / quoting)
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=desc,
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=60,
        )
    except Exception:
        logger.exception("self_repair: 'graph bead' invocation failed (detector=%s)", detector)
        return None
    if proc.returncode != 0:
        logger.error(
            "self_repair: 'graph bead' rc=%s detector=%s stderr=%s",
            proc.returncode, detector, (proc.stderr or "")[:500],
        )
        return None
    bead_id = _parse_bead_id(proc.stdout)
    logger.info(
        "self_repair: filed bead=%s detector=%s expected=%s sig=%s",
        bead_id, detector, expected, sig,
    )
    return bead_id


async def report_detector_miss(
    detector: str,
    *,
    expected: str,
    evidence: str,
    evidence_kind: str = "screen_capture",
    code_ref: str | None = None,
    context: dict | None = None,
    cooldown_s: float = _DEFAULT_COOLDOWN_S,
    _now: float | None = None,
) -> str | None:
    """File a self-repair bead for a detector that timed out without its
    expected event.

    Idempotent within ``cooldown_s`` per (detector, evidence signature).
    Returns the bead id, or None if deduped or filing failed. **Never
    raises** — safe to ``await`` from inside a poll loop without a guard.

    Args:
        detector: stable detector name, e.g. ``"claude_screen_state"``.
        expected: the event that didn't happen, e.g. ``"composer_ready"``.
        evidence: the captured-but-unparsed input (pane text, raw line, ...).
        evidence_kind: ``"screen_capture"`` | ``"jsonl_line"`` | ...
        code_ref: ``file:symbol`` pointer to the detector for the librarian.
        context: extra fields surfaced in the bead (session, elapsed, ...).
        cooldown_s: dedup window for an identical miss.
    """
    try:
        sig = _signature(detector, evidence)
        now = _now if _now is not None else time.time()
        if _already_filed(detector, sig, cooldown_s, now):
            return None
        # Record the attempt BEFORE filing so a slow/duplicate concurrent
        # poll doesn't double-file while the first is in flight.
        _FILED[(detector, sig)] = now
        return await asyncio.to_thread(
            _file_bead, detector, expected, evidence, evidence_kind, code_ref, context, sig
        )
    except Exception:
        logger.exception("self_repair: report_detector_miss failed (detector=%s)", detector)
        return None


def reset_dedup_cache() -> None:
    """Test helper — clear the process-local dedup cache."""
    _FILED.clear()
