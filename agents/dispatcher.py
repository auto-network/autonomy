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

# Map labels to container images. Beads with these labels get dispatched
# to specialized images with the right dependencies baked in.
LABEL_IMAGE_MAP = {
    "dashboard": "autonomy-agent:dashboard",
    # Add more as project images are created:
    # "scraper": "autonomy-agent:scraper",
}
DEFAULT_IMAGE = "autonomy-agent"


@dataclass
class DispatchResult:
    bead_id: str
    exit_code: int
    decision: dict | None = None
    output_dir: str = ""
    error: str = ""
    commit_hash: str = ""
    worktree_path: str = ""
    branch: str = ""


@dataclass
class DispatcherConfig:
    max_concurrent: int = 1  # Start simple — one at a time
    label_filter: str = "implementation"  # Which queue to pull from
    dry_run: bool = False
    interval: int = 60  # Seconds between dispatch cycles
    loop: bool = False


def run_cmd(cmd: list[str], timeout: int = 15) -> str:
    """Run any command and return stdout."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
            cwd=str(REPO_ROOT),
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  cmd error: {e}", file=sys.stderr)
        return ""


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
    """Get beads approved for dispatch, optionally filtered by queue label.

    Queries for readiness:approved — the single human gate.
    The readiness dimension (idea → draft → specified → approved) is set
    via bd set-state; the dispatcher only picks up approved beads.
    """
    if label_filter:
        out = run_bd(["query", f'status=open AND label={label_filter} AND label="readiness:approved"', "--json"])
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


def set_dispatch_state(bead_id: str, state: str, reason: str = "") -> None:
    """Set the dispatch dimension on a bead.

    Dispatch states: queued, launching, running, collecting, merging, done, failed.
    """
    reason_text = reason or f"dispatch:{state}"
    run_bd(["set-state", bead_id, f"dispatch={state}", "--reason", reason_text])


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

    # Set status to in_progress so dashboard shows it
    run_bd(["update", bead_id, "-s", "in_progress"])

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


def image_for_bead(bead: dict) -> str:
    """Select the container image based on bead labels."""
    labels = bead.get("labels") or []
    for label in labels:
        if label in LABEL_IMAGE_MAP:
            return LABEL_IMAGE_MAP[label]
    return DEFAULT_IMAGE


def launch_agent(bead_id: str, image: str = DEFAULT_IMAGE) -> DispatchResult:
    """Launch an agent container for a bead and collect results."""
    print(f"  Launching agent for {bead_id} (image: {image})...")

    result = subprocess.run(
        [str(LAUNCH_SCRIPT), bead_id, f"--image={image}"],
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

    # Read commit hash and worktree path from output dir
    commit_hash = ""
    worktree_path = ""
    branch = ""
    if output_dir:
        commit_file = Path(output_dir) / ".commit_hash"
        if commit_file.exists():
            commit_hash = commit_file.read_text().strip()
        worktree_file = Path(output_dir) / ".worktree_path"
        if worktree_file.exists():
            worktree_path = worktree_file.read_text().strip()
        branch_file = Path(output_dir) / ".branch"
        if branch_file.exists():
            branch = branch_file.read_text().strip()

    return DispatchResult(
        bead_id=bead_id,
        exit_code=result.returncode,
        decision=decision,
        output_dir=output_dir,
        error=result.stderr if result.returncode != 0 else "",
        commit_hash=commit_hash,
        worktree_path=worktree_path,
        branch=branch,
    )


def cleanup_worktree(worktree_path: str) -> None:
    """Remove a git worktree after dispatch."""
    if worktree_path and Path(worktree_path).exists():
        subprocess.run(
            ["git", "worktree", "remove", worktree_path, "--force"],
            capture_output=True, text=True, timeout=15,
            cwd=str(REPO_ROOT),
        )


def process_decision(dispatch_result: DispatchResult) -> None:
    """Process agent decision and update bead state."""
    bead_id = dispatch_result.bead_id
    decision = dispatch_result.decision

    # Record commit hash on bead if agent committed
    if dispatch_result.commit_hash:
        commit = dispatch_result.commit_hash[:10]
        branch = dispatch_result.branch
        print(f"  Commit: {commit} on {branch}")
        run_bd(["update", bead_id, "--append-notes",
                f"commit: {dispatch_result.commit_hash} branch: {branch}"])

    if decision is None:
        print(f"  No decision file from {bead_id} (exit code {dispatch_result.exit_code})")
        set_dispatch_state(bead_id, "failed", f"No decision file. Exit code: {dispatch_result.exit_code}")
        release_bead(bead_id, "FAILED", f"No decision file. Exit code: {dispatch_result.exit_code}")
        cleanup_worktree(dispatch_result.worktree_path)
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

    # Auto-merge to master on DONE if agent committed
    if status == "DONE" and dispatch_result.commit_hash and dispatch_result.branch:
        set_dispatch_state(bead_id, "merging")
        merge_result = subprocess.run(
            ["git", "merge", dispatch_result.branch,
             "--no-edit", "-m",
             f"merge: {bead_id} — {reason}"],
            capture_output=True, text=True, timeout=30,
            cwd=str(REPO_ROOT),
        )
        if merge_result.returncode == 0:
            merge_hash = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
                cwd=str(REPO_ROOT),
            ).stdout.strip()
            print(f"  Merged to master: {merge_hash[:10]}")
            run_bd(["update", bead_id, "--append-notes",
                    f"merged: {merge_hash}"])
        else:
            print(f"  Merge conflict — leaving branch for manual review")
            print(f"    {merge_result.stderr.strip()}")
            # Abort the failed merge
            subprocess.run(
                ["git", "merge", "--abort"],
                capture_output=True, text=True, timeout=5,
                cwd=str(REPO_ROOT),
            )
            run_bd(["update", bead_id, "--append-notes",
                    f"merge conflict on {dispatch_result.branch} — needs manual review"])
            # Override status to BLOCKED since code can't integrate
            status = "BLOCKED"
            reason = f"Merge conflict: {merge_result.stderr.strip()[:200]}"

    # Set final dispatch state
    final_dispatch = "done" if status == "DONE" else "failed"
    set_dispatch_state(bead_id, final_dispatch, reason)

    # Release the bead with appropriate state
    release_bead(bead_id, status, reason)

    # Clean up worktree (branch persists for review)
    cleanup_worktree(dispatch_result.worktree_path)

    # Delete branch if it was merged successfully
    if status == "DONE" and dispatch_result.branch:
        subprocess.run(
            ["git", "branch", "-d", dispatch_result.branch],
            capture_output=True, text=True, timeout=5,
            cwd=str(REPO_ROOT),
        )


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

    # 4. Pick highest priority (lowest number)
    available.sort(key=lambda b: b.get("priority", 99))
    bead = available[0]
    bead_id = bead["id"]
    title = bead.get("title", "?")

    image = image_for_bead(bead)
    print(f"  Selected: {bead_id} — {title} (P{bead.get('priority', '?')}) [{image}]")

    if config.dry_run:
        print("  [DRY RUN] Would dispatch this bead")
        return 0

    # 5. Claim + dispatch=queued
    if not claim_bead(bead_id):
        return 0
    set_dispatch_state(bead_id, "queued")

    # 6. Launch (dispatch=launching → running)
    set_dispatch_state(bead_id, "launching", f"image:{image}")
    try:
        set_dispatch_state(bead_id, "running")
        result = launch_agent(bead_id, image=image)
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT: Agent exceeded 10 minute limit")
        set_dispatch_state(bead_id, "failed", "Agent timeout (10 min)")
        release_bead(bead_id, "FAILED", "Agent timeout (10 min)")
        return 1

    # 7. Collect results (dispatch=collecting)
    set_dispatch_state(bead_id, "collecting")
    process_decision(result)

    # 8. Ingest agent session into graph and link to bead
    subprocess.run(
        ["graph", "sessions", "--all"],
        capture_output=True, text=True, timeout=30,
        cwd=str(REPO_ROOT),
    )

    # Find the ingested session and link it to the bead
    if result.output_dir:
        session_dir = Path(result.output_dir) / "sessions"
        jsonl_files = list(session_dir.glob("**/*.jsonl")) if session_dir.exists() else []
        if jsonl_files:
            # Search graph for the session by path fragment
            session_name = jsonl_files[0].stem  # UUID of the session
            search_out = run_cmd(
                ["graph", "search", session_name, "--json", "--limit", "1"]
            )
            if search_out:
                try:
                    hits = json.loads(search_out)
                    if isinstance(hits, list) and hits:
                        src_id = hits[0].get("source_id", hits[0].get("id", ""))
                        if src_id:
                            subprocess.run(
                                ["graph", "link", result.bead_id, src_id,
                                 "-r", "implemented_by"],
                                capture_output=True, text=True, timeout=15,
                                cwd=str(REPO_ROOT),
                            )
                            print(f"  Linked {result.bead_id} → {src_id} (implemented_by)")
                except json.JSONDecodeError:
                    pass

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
