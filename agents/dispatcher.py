"""Autonomy Dispatcher — deterministic claim/release/close loop.

Owns all bead state mutations. Agents run --readonly.
No LLM in this loop — just a state machine.

Usage:
    python -m agents.dispatcher                  # Run one dispatch cycle
    python -m agents.dispatcher --loop           # Run continuously
    python -m agents.dispatcher --loop --interval 30
    python -m agents.dispatcher --dry-run        # Show what would be dispatched
"""

from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCH_SCRIPT = Path(__file__).parent / "launch.sh"


@dataclass
class DispatchResult:
    bead_id: str
    exit_code: int
    decision: dict | None = None
    output_dir: str = ""
    error: str = ""


@dataclass
class DispatcherConfig:
    max_concurrent: int = 1  # Start simple — one at a time
    label_filter: str = "implementation"  # Which queue to pull from
    dry_run: bool = False
    interval: int = 60  # Seconds between dispatch cycles
    loop: bool = False


def run_bd(args: list[str], timeout: int = 15) -> str:
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


def get_ready_beads(label_filter: str | None = None) -> list[dict]:
    """Get ready beads, optionally filtered by label.

    Uses bd query with label filter since bd ready --json doesn't include labels.
    Only returns beads that are both unblocked (open) and match the label.
    """
    if label_filter:
        # bd query supports label filtering and returns full bead data
        out = run_bd(["query", f"status=open AND label={label_filter}", "--json"])
    else:
        out = run_bd(["ready", "--json"])

    if not out:
        return []
    try:
        beads = json.loads(out)
    except json.JSONDecodeError:
        return []

    if not isinstance(beads, list):
        return []

    return beads


def get_claimed_beads() -> set[str]:
    """Get IDs of beads currently claimed by the dispatcher."""
    out = run_bd(["query", "label=work:claimed", "--json"])
    if not out:
        return set()
    try:
        beads = json.loads(out)
        return {b["id"] for b in beads if isinstance(b, dict)}
    except (json.JSONDecodeError, KeyError):
        return set()


def claim_bead(bead_id: str) -> bool:
    """Claim a bead for dispatch. Returns True if successful."""
    result = subprocess.run(
        ["bd", "set-state", bead_id, "work=claimed",
         "--reason", f"dispatcher:{os.getpid()}"],
        capture_output=True, text=True, timeout=15,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        print(f"  Failed to claim {bead_id}: {result.stderr}", file=sys.stderr)
        return False

    # Also add label for queryability
    subprocess.run(
        ["bd", "label", "add", bead_id, "work:claimed"],
        capture_output=True, text=True, timeout=15,
        cwd=str(REPO_ROOT),
    )
    return True


def release_bead(bead_id: str, status: str, reason: str) -> None:
    """Release a bead after agent completion."""
    # Remove claim label
    subprocess.run(
        ["bd", "label", "remove", bead_id, "work:claimed"],
        capture_output=True, text=True, timeout=15,
        cwd=str(REPO_ROOT),
    )

    if status == "DONE":
        run_bd(["close", bead_id, "--reason", reason])
    elif status == "BLOCKED":
        run_bd(["set-state", bead_id, "work=blocked", "--reason", reason])
        run_bd(["update", bead_id, "--append-notes", f"Blocked: {reason}"])
    elif status == "FAILED":
        run_bd(["set-state", bead_id, "work=failed", "--reason", reason])
        run_bd(["update", bead_id, "--append-notes", f"Failed: {reason}"])
    else:
        # Unknown status — log and release
        run_bd(["set-state", bead_id, "work=released", "--reason", f"Unknown status: {status}"])


def launch_agent(bead_id: str) -> DispatchResult:
    """Launch an agent container for a bead and collect results."""
    print(f"  Launching agent for {bead_id}...")

    result = subprocess.run(
        [str(LAUNCH_SCRIPT), bead_id],
        capture_output=True, text=True,
        timeout=600,  # 10 minute max per agent run
        cwd=str(REPO_ROOT),
        env={**os.environ, "BD_READONLY": "1"},
    )

    # Find output directory from stdout
    output_dir = ""
    for line in result.stdout.splitlines():
        if "Output:" in line and "agent-runs" in line:
            output_dir = line.split("Output:")[-1].strip()
            break

    # Try to read decision file
    decision = None
    if output_dir:
        decision_path = Path(output_dir) / "decision.json"
        if decision_path.exists():
            try:
                decision = json.loads(decision_path.read_text())
            except json.JSONDecodeError:
                pass

    return DispatchResult(
        bead_id=bead_id,
        exit_code=result.returncode,
        decision=decision,
        output_dir=output_dir,
        error=result.stderr if result.returncode != 0 else "",
    )


def process_decision(dispatch_result: DispatchResult) -> None:
    """Process agent decision and update bead state."""
    bead_id = dispatch_result.bead_id
    decision = dispatch_result.decision

    if decision is None:
        print(f"  No decision file from {bead_id} (exit code {dispatch_result.exit_code})")
        release_bead(bead_id, "FAILED", f"No decision file. Exit code: {dispatch_result.exit_code}")
        return

    status = decision.get("status", "FAILED")
    reason = decision.get("reason", "No reason provided")
    notes = decision.get("notes", "")

    print(f"  Decision: {status} — {reason}")

    # Append agent notes to bead
    if notes:
        run_bd(["update", bead_id, "--append-notes", notes])

    # Create discovered beads
    for new_bead in decision.get("discovered_beads", []):
        title = new_bead.get("title", "Untitled")
        desc = new_bead.get("description", "")
        labels = new_bead.get("labels", ["refinement"])
        priority = new_bead.get("priority", 2)

        label_args = ["-l", ",".join(labels)] if labels else []
        out = run_bd([
            "create", title,
            "-d", desc,
            "-p", str(priority),
            *label_args,
        ])
        if out:
            print(f"  Created discovered bead: {out}")

    # Release the bead with appropriate state
    release_bead(bead_id, status, reason)


def dispatch_cycle(config: DispatcherConfig) -> int:
    """Run one dispatch cycle. Returns number of beads dispatched."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{timestamp}] Dispatch cycle (queue: {config.label_filter})")

    # 1. Get ready beads
    ready = get_ready_beads(config.label_filter)
    if not ready:
        print("  No ready beads in queue")
        return 0

    # 2. Filter out already-claimed beads
    claimed = get_claimed_beads()
    available = [b for b in ready if b.get("id") not in claimed]

    if not available:
        print(f"  {len(ready)} ready but all claimed")
        return 0

    print(f"  {len(available)} available beads")

    # 3. Pick highest priority (lowest number)
    available.sort(key=lambda b: b.get("priority", 99))
    bead = available[0]
    bead_id = bead["id"]
    title = bead.get("title", "?")

    print(f"  Selected: {bead_id} — {title} (P{bead.get('priority', '?')})")

    if config.dry_run:
        print("  [DRY RUN] Would dispatch this bead")
        return 0

    # 4. Claim
    if not claim_bead(bead_id):
        return 0

    # 5. Launch
    try:
        result = launch_agent(bead_id)
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT: Agent exceeded 10 minute limit")
        release_bead(bead_id, "FAILED", "Agent timeout (10 min)")
        return 1

    # 6. Process decision
    process_decision(result)

    # 7. Ingest agent session into graph
    subprocess.run(
        ["graph", "sessions", "--all"],
        capture_output=True, text=True, timeout=30,
        cwd=str(REPO_ROOT),
    )

    return 1


def main():
    parser = argparse.ArgumentParser(description="Autonomy Dispatcher")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between cycles (default: 60)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be dispatched")
    parser.add_argument("--queue", default="implementation", help="Label queue to pull from (default: implementation)")
    parser.add_argument("--max-concurrent", type=int, default=1, help="Max concurrent agents (default: 1)")

    args = parser.parse_args()
    config = DispatcherConfig(
        max_concurrent=args.max_concurrent,
        label_filter=args.queue,
        dry_run=args.dry_run,
        interval=args.interval,
        loop=args.loop,
    )

    print(f"Autonomy Dispatcher (pid={os.getpid()})")
    print(f"  Queue: {config.label_filter}")
    print(f"  Max concurrent: {config.max_concurrent}")
    print(f"  Loop: {config.loop} (interval: {config.interval}s)")

    if config.loop:
        while True:
            try:
                dispatch_cycle(config)
                time.sleep(config.interval)
            except KeyboardInterrupt:
                print("\nDispatcher stopped.")
                break
    else:
        dispatch_cycle(config)


if __name__ == "__main__":
    main()
