"""Bead readiness gate — draft/spec-complete/ready lifecycle.

Formalizes the bar for dispatching a bead. The dispatcher only picks up
beads with readiness=ready. This is orthogonal to `bd ready` (which
checks dependency blockers) and to `bd lint` (which checks template
sections). The readiness gate checks that a bead is *well-specified
enough* to dispatch to an autonomous agent.

Lifecycle:
    draft → spec-complete → ready
    (any)   → draft   (demote back if spec degrades)

Usage:
    # Check readiness of a bead
    python -m agents.readiness check <bead-id>

    # Promote a bead to the next readiness level
    python -m agents.readiness promote <bead-id>

    # Set readiness directly (e.g., demote)
    python -m agents.readiness set <bead-id> draft

    # Check what's missing for promotion
    python -m agents.readiness gaps <bead-id>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Readiness levels in lifecycle order
READINESS_LEVELS = ("idea", "draft", "specified", "approved")

# The label dimension used with bd set-state / bd query
READINESS_DIMENSION = "readiness"


@dataclass
class ReadinessCheck:
    """Result of checking a bead's readiness."""
    bead_id: str
    current_level: str  # Current readiness label, or "draft" if unset
    target_level: str   # What we're checking against
    passed: bool
    gaps: list[str] = field(default_factory=list)  # What's missing
    warnings: list[str] = field(default_factory=list)  # Non-blocking issues


def _run_bd(args: list[str], timeout: int = 15) -> str:
    """Run a bd command and return stdout."""
    try:
        result = subprocess.run(
            ["bd"] + args,
            capture_output=True, text=True, timeout=timeout,
            cwd=str(REPO_ROOT),
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  bd error: {e}", file=sys.stderr)
        return ""


def get_bead(bead_id: str) -> dict | None:
    """Get bead details from bd show --json."""
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


def get_readiness_level(bead: dict) -> str:
    """Extract current readiness level from bead labels.

    Looks for labels matching 'readiness:<level>'. Returns 'idea'
    if no readiness label is set (implicit default).
    """
    labels = bead.get("labels") or []
    for label in labels:
        if label.startswith(f"{READINESS_DIMENSION}:"):
            level = label.split(":", 1)[1]
            if level in READINESS_LEVELS:
                return level
    return "idea"


def check_specified(bead: dict) -> ReadinessCheck:
    """Check if a bead meets specified criteria.

    Specified means the bead is well-specified enough that an agent
    *could* work on it. Requirements:
    - Has a non-trivial description (>50 chars)
    - Has acceptance criteria or design notes
    - Has a meaningful title (>10 chars)
    """
    bead_id = bead.get("id", "?")
    gaps = []
    warnings = []

    title = bead.get("title", "")
    description = bead.get("description", "")
    acceptance = bead.get("acceptance_criteria", "")
    design = bead.get("design", "")

    # Title check
    if len(title) < 10:
        gaps.append(f"Title too short ({len(title)} chars, need ≥10)")

    # Description check
    if not description:
        gaps.append("Missing description")
    elif len(description) < 50:
        gaps.append(f"Description too short ({len(description)} chars, need ≥50)")

    # Must have either acceptance criteria or design notes
    if not acceptance and not design:
        gaps.append("Missing both acceptance_criteria and design — need at least one")

    # Warnings (non-blocking)
    if not acceptance:
        warnings.append("No acceptance_criteria — consider adding for clarity")
    if not design:
        warnings.append("No design notes — consider adding implementation hints")

    return ReadinessCheck(
        bead_id=bead_id,
        current_level=get_readiness_level(bead),
        target_level="specified",
        passed=len(gaps) == 0,
        gaps=gaps,
        warnings=warnings,
    )


# Keep backward-compatible alias
check_spec_complete = check_specified


def check_approved(bead: dict) -> ReadinessCheck:
    """Check if a bead meets approved-for-dispatch criteria.

    Approved means the bead is fully prepared for autonomous agent execution.
    Builds on specified and adds:
    - Must already be specified (all specified checks pass)
    - Must have the 'implementation' label (dispatch queue)
    - Priority must be set (not None/missing)

    Note: the 'approved' label itself is not checked here — approval is the
    human action of setting readiness=approved via set-state. This function
    checks whether the bead *qualifies* for approval.
    """
    bead_id = bead.get("id", "?")
    gaps = []
    warnings = []

    # First, must pass specified
    spec_check = check_specified(bead)
    if not spec_check.passed:
        gaps.append("Does not meet specified criteria:")
        for g in spec_check.gaps:
            gaps.append(f"  - {g}")

    labels = bead.get("labels") or []

    # Must be in implementation queue
    if "implementation" not in labels:
        gaps.append("Missing 'implementation' label — not queued for dispatch")

    # Priority must be set
    priority = bead.get("priority")
    if priority is None:
        gaps.append("Priority not set")

    # Type should be set
    if not bead.get("issue_type"):
        warnings.append("No issue_type set — consider adding for routing")

    return ReadinessCheck(
        bead_id=bead_id,
        current_level=get_readiness_level(bead),
        target_level="approved",
        passed=len(gaps) == 0,
        gaps=gaps,
        warnings=warnings + spec_check.warnings,
    )


# Keep backward-compatible alias
check_ready = check_approved


def check_readiness(bead: dict, target: str = "approved") -> ReadinessCheck:
    """Check if a bead meets the specified readiness level."""
    if target == "specified":
        return check_specified(bead)
    elif target == "approved":
        return check_approved(bead)
    else:
        return ReadinessCheck(
            bead_id=bead.get("id", "?"),
            current_level=get_readiness_level(bead),
            target_level=target,
            passed=target in ("idea", "draft"),  # idea/draft are always met
            gaps=[] if target in ("idea", "draft") else [f"Unknown readiness level: {target}"],
        )


def is_dispatch_ready(bead: dict) -> bool:
    """Quick check: is this bead approved for dispatch?

    Returns True only if the bead has readiness:approved label.
    Note: the dispatcher no longer imports this — it queries
    label=readiness:approved directly. Kept for CLI/programmatic use.
    """
    return get_readiness_level(bead) == "approved"


def set_readiness(bead_id: str, level: str, reason: str = "") -> bool:
    """Set the readiness level of a bead via set-state.

    Returns True if successful.
    """
    if level not in READINESS_LEVELS:
        print(f"Invalid readiness level: {level}. Must be one of {READINESS_LEVELS}",
              file=sys.stderr)
        return False

    reason_text = reason or f"readiness set to {level}"
    result = subprocess.run(
        ["bd", "set-state", bead_id, f"{READINESS_DIMENSION}={level}",
         "--reason", reason_text],
        capture_output=True, text=True, timeout=15,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        print(f"Failed to set readiness: {result.stderr}", file=sys.stderr)
        return False
    return True


def promote_readiness(bead_id: str) -> ReadinessCheck | None:
    """Promote a bead to the next readiness level if it qualifies.

    Returns the readiness check result, or None if already at max level.
    """
    bead = get_bead(bead_id)
    if not bead:
        print(f"Bead not found: {bead_id}", file=sys.stderr)
        return None

    current = get_readiness_level(bead)
    current_idx = READINESS_LEVELS.index(current)

    if current_idx >= len(READINESS_LEVELS) - 1:
        print(f"{bead_id} is already at max readiness: {current}")
        return ReadinessCheck(
            bead_id=bead_id,
            current_level=current,
            target_level=current,
            passed=True,
        )

    next_level = READINESS_LEVELS[current_idx + 1]
    check = check_readiness(bead, next_level)

    if check.passed:
        if set_readiness(bead_id, next_level, f"promoted from {current}"):
            check.current_level = next_level
            print(f"{bead_id}: {current} → {next_level}")
        else:
            check.passed = False
            check.gaps.append("set-state command failed")
    else:
        print(f"{bead_id}: cannot promote from {current} to {next_level}")
        for gap in check.gaps:
            print(f"  - {gap}")

    return check


def format_check(check: ReadinessCheck) -> str:
    """Format a readiness check result for display."""
    lines = []
    status = "✓ PASS" if check.passed else "✗ FAIL"
    lines.append(f"{check.bead_id}: {status} for {check.target_level} (current: {check.current_level})")

    if check.gaps:
        lines.append("  Gaps:")
        for gap in check.gaps:
            lines.append(f"    - {gap}")

    if check.warnings:
        lines.append("  Warnings:")
        for w in check.warnings:
            lines.append(f"    ⚠ {w}")

    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────

def cmd_check(args: argparse.Namespace) -> int:
    """Check readiness of a bead."""
    bead = get_bead(args.bead_id)
    if not bead:
        print(f"Bead not found: {args.bead_id}", file=sys.stderr)
        return 1

    target = args.level or "approved"
    check = check_readiness(bead, target)
    print(format_check(check))

    if args.json:
        print(json.dumps({
            "bead_id": check.bead_id,
            "current_level": check.current_level,
            "target_level": check.target_level,
            "passed": check.passed,
            "gaps": check.gaps,
            "warnings": check.warnings,
        }, indent=2))

    return 0 if check.passed else 1


def cmd_promote(args: argparse.Namespace) -> int:
    """Promote a bead to next readiness level."""
    result = promote_readiness(args.bead_id)
    if result is None:
        return 1
    return 0 if result.passed else 1


def cmd_set(args: argparse.Namespace) -> int:
    """Set readiness level directly."""
    ok = set_readiness(args.bead_id, args.level, args.reason or "")
    return 0 if ok else 1


def cmd_gaps(args: argparse.Namespace) -> int:
    """Show what's missing for a bead to reach next level."""
    bead = get_bead(args.bead_id)
    if not bead:
        print(f"Bead not found: {args.bead_id}", file=sys.stderr)
        return 1

    current = get_readiness_level(bead)
    current_idx = READINESS_LEVELS.index(current)

    if current_idx >= len(READINESS_LEVELS) - 1:
        print(f"{args.bead_id} is already at max readiness: {current}")
        return 0

    next_level = READINESS_LEVELS[current_idx + 1]
    check = check_readiness(bead, next_level)
    print(format_check(check))
    return 0 if check.passed else 1


def main():
    parser = argparse.ArgumentParser(
        description="Bead readiness gate — draft/spec-complete/ready lifecycle")
    subs = parser.add_subparsers(dest="command", required=True)

    # check
    p_check = subs.add_parser("check", help="Check readiness of a bead")
    p_check.add_argument("bead_id")
    p_check.add_argument("--level", choices=READINESS_LEVELS,
                         help="Level to check against (default: ready)")
    p_check.add_argument("--json", action="store_true", help="JSON output")
    p_check.set_defaults(func=cmd_check)

    # promote
    p_promote = subs.add_parser("promote", help="Promote to next readiness level")
    p_promote.add_argument("bead_id")
    p_promote.set_defaults(func=cmd_promote)

    # set
    p_set = subs.add_parser("set", help="Set readiness level directly")
    p_set.add_argument("bead_id")
    p_set.add_argument("level", choices=READINESS_LEVELS)
    p_set.add_argument("--reason", help="Reason for the change")
    p_set.set_defaults(func=cmd_set)

    # gaps
    p_gaps = subs.add_parser("gaps", help="Show gaps to next readiness level")
    p_gaps.add_argument("bead_id")
    p_gaps.set_defaults(func=cmd_gaps)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
