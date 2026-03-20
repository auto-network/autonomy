"""Dynamic primer builder for the experience report reviewer librarian.

Called by the dispatcher at launch time to assemble bead-specific context
for the experience reviewer agent.

Usage:
    from agents.librarians.experience_reviewer.primer import build_primer
    text = build_primer(payload)

Payload keys:
    bead_id       — the bead that was dispatched
    report_path   — path to the agent's experience_report.md
    decision_path — path to the agent's decision.json
    run_id        — the dispatch run ID (output dir name)
"""

from __future__ import annotations
import json
import subprocess
from pathlib import Path


def _run_bd(args: list[str], timeout: int = 15) -> str:
    """Run a bd CLI command and return stdout. Returns empty string on failure."""
    try:
        result = subprocess.run(
            ["bd"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _get_bead(bead_id: str) -> dict | None:
    """Fetch bead details via bd show --json. Returns None on failure."""
    out = _run_bd(["show", bead_id, "--json"])
    if not out:
        return None
    try:
        data = json.loads(out)
        if isinstance(data, list) and data:
            return data[0]
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _read_file(path: str | Path | None) -> str | None:
    """Read a file, returning None if missing or unreadable."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _format_decision(decision: dict) -> str:
    """Format decision.json contents as a readable section."""
    lines = []

    status = decision.get("status", "unknown")
    reason = decision.get("reason", "")
    lines.append(f"**Status:** {status}")
    if reason:
        lines.append(f"**Reason:** {reason}")

    scores = decision.get("scores", {})
    if scores:
        score_parts = []
        for key in ("tooling", "clarity", "confidence"):
            if key in scores:
                score_parts.append(f"{key}={scores[key]}/5")
        if score_parts:
            lines.append(f"**Scores:** {', '.join(score_parts)}")

    time_breakdown = decision.get("time_breakdown", {})
    if time_breakdown:
        tb_parts = []
        for key, label in [
            ("research_pct", "research"),
            ("coding_pct", "coding"),
            ("debugging_pct", "debugging"),
            ("tooling_workaround_pct", "tooling workaround"),
        ]:
            if key in time_breakdown:
                tb_parts.append(f"{label} {time_breakdown[key]}%")
        if tb_parts:
            lines.append(f"**Time breakdown:** {', '.join(tb_parts)}")

    discovered = decision.get("discovered_beads", [])
    if discovered:
        lines.append(f"\n**Discovered beads** ({len(discovered)} item(s) — already filed by dispatcher, skip these):")
        for item in discovered:
            title = item.get("title", "?")
            priority = item.get("priority", "?")
            labels = ", ".join(item.get("labels", []))
            desc = item.get("description", "")
            lines.append(f"  - [{labels}] P{priority}: {title}")
            if desc:
                lines.append(f"    {desc[:200]}")

    notes = decision.get("notes", "")
    if notes:
        lines.append(f"\n**Dispatcher notes:** {notes}")

    return "\n".join(lines)


def _format_smoke_result(smoke: dict) -> str:
    """Format smoke_result.json as a readable one-liner summary."""
    if not smoke:
        return ""
    t1 = smoke.get("tier1")
    t2 = smoke.get("tier2")
    dur_s = (smoke["duration_ms"] / 1000) if smoke.get("duration_ms") is not None else None
    dur_str = f"{dur_s:.1f}s" if dur_s is not None else ""

    if not t1 and t2 and t2.get("skipped"):
        return f"~ Smoke skipped ({t2.get('reason', 'tier2')})"

    t1_checks = (t1 or {}).get("checks", [])
    t1_pass = sum(1 for c in t1_checks if c.get("pass"))
    t1_total = len(t1_checks)
    t2_pages = (t2 or {}).get("pages") if t2 and not t2.get("skipped") else None
    t2_pass = sum(1 for p in t2_pages if p.get("pass")) if t2_pages else None
    t2_total = len(t2_pages) if t2_pages else None
    t2_skipped = t2 and t2.get("skipped")

    if smoke.get("pass"):
        parts = ["PASS"]
        if t1_total:
            parts.append(f"tier1 {t1_pass}/{t1_total}")
        if t2_pages:
            parts.append(f"tier2 {t2_pass}/{t2_total}")
        elif t2_skipped:
            parts.append("tier2 skipped")
        if dur_str:
            parts.append(dur_str)
        return "✓ Smoke " + "  ".join(parts)
    else:
        fail_detail = ""
        failing = next((c for c in t1_checks if not c.get("pass")), None)
        if failing:
            fail_detail = failing.get("detail") or failing.get("name", "")
        elif t2_pages:
            fail_page = next((p for p in t2_pages if not p.get("pass")), None)
            if fail_page:
                fail_detail = fail_page.get("detail") or fail_page.get("page", "")
        parts = ["FAIL"]
        if t1_total:
            parts.append(f"tier1 {t1_pass}/{t1_total}")
        if t2_pages:
            parts.append(f"tier2 {t2_pass}/{t2_total}")
        if fail_detail:
            parts.append(fail_detail)
        return "✗ Smoke " + "  ".join(parts)


def build_primer(payload: dict) -> str:
    """Build a context primer for the experience reviewer agent.

    Reads bead details, experience report, decision.json, and smoke_result.json
    from the payload paths and assembles a structured markdown briefing.

    Args:
        payload: dict with keys bead_id, report_path, decision_path, run_id

    Returns:
        Markdown string ready to prepend to the agent prompt.
    """
    bead_id = payload.get("bead_id", "unknown")
    report_path = payload.get("report_path")
    decision_path = payload.get("decision_path")
    run_id = payload.get("run_id", bead_id)

    sections: list[str] = []

    # ── Header ───────────────────────────────────────────────────
    sections.append(f"# Experience Report Review: {bead_id}")
    sections.append(f"**Run:** {run_id}")

    # ── 1. Bead context ──────────────────────────────────────────
    bead = _get_bead(bead_id)
    if bead:
        title = bead.get("title", bead_id)
        priority = bead.get("priority", "?")
        status = bead.get("status", "?")
        sections.append(f"\n## Dispatched Bead")
        sections.append(f"**Title:** {title}  **Priority:** P{priority}  **Status:** {status}")
        desc = bead.get("description", "")
        if desc:
            sections.append(f"\n**Description:**\n{desc}")
        ac = bead.get("acceptance_criteria", "")
        if ac:
            sections.append(f"\n**Acceptance Criteria:**\n{ac}")
    else:
        sections.append(f"\n## Dispatched Bead")
        sections.append(f"(Could not fetch bead details for {bead_id} — bd show returned nothing)")

    # ── 2. Decision summary ──────────────────────────────────────
    decision_text = _read_file(decision_path)
    if decision_text:
        try:
            decision = json.loads(decision_text)
            sections.append(f"\n## Agent Decision")
            sections.append(_format_decision(decision))
        except json.JSONDecodeError:
            sections.append(f"\n## Agent Decision")
            sections.append(f"(decision.json could not be parsed — raw content follows)")
            sections.append(f"```\n{decision_text[:500]}\n```")
    else:
        sections.append(f"\n## Agent Decision")
        sections.append(f"(No decision.json found at `{decision_path}`)")

    # ── 3. Smoke result ───────────────────────────────────────────
    smoke_path = Path(decision_path).parent / "smoke_result.json" if decision_path else None
    smoke_text = _read_file(smoke_path)
    if smoke_text:
        try:
            smoke = json.loads(smoke_text)
            smoke_summary = _format_smoke_result(smoke)
            sections.append(f"\n## Smoke Test Result")
            sections.append(smoke_summary)
        except json.JSONDecodeError:
            pass

    # ── 4. Experience report ─────────────────────────────────────
    report_text = _read_file(report_path)
    if report_text:
        sections.append(f"\n## Experience Report")
        sections.append(report_text.strip())
    else:
        sections.append(f"\n## Experience Report")
        if report_path:
            sections.append(
                f"(No experience report found at `{report_path}`. "
                "The agent may have crashed or exited without writing the report.)"
            )
        else:
            sections.append("(No report_path provided in payload.)")

    # ── 5. Instructions ──────────────────────────────────────────
    sections.append(f"""
## Your Task

Review the experience report above for bead `{bead_id}` and extract actionable items
into the knowledge graph. Follow the rules in your role definition.

Key commands:
```
bd show {bead_id}                          # Read full bead details
graph search "<query>" --or --limit 5     # Check for duplicates before creating
graph note "pitfall: ..." --tags pitfall,<topics>   # Record a pitfall
graph bead "Fix: ..." -p 2 -d - < /tmp/desc.txt    # Create a bug bead
graph bead "Title" -p 2 -d - < /tmp/desc.txt       # Create discovered work bead
```

Remember:
- Search before creating anything — no duplicates
- Skip items already in the discovered_beads list above (dispatcher will create them)
- Skip vague items with no actionable detail
- Write your summary to stdout when done
""")

    return "\n\n".join(sections)
